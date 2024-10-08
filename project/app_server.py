import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
from langchain_community.llms import Ollama
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain.prompts import ChatPromptTemplate
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.vectorstores import FAISS
from langchain.chains import RetrievalQA
from dotenv import load_dotenv
import uvicorn
import logging
import requests
import argparse 
from collections import deque
import subprocess
import json
from langchain.schema.runnable import RunnableLambda, RunnablePassthrough, RunnableSequence
from langchain.schema.output_parser import StrOutputParser
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
from langchain.chains import LLMChain
from langchain.schema.runnable import RunnablePassthrough


import torch
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = FastAPI()

# Set up CORS
origins = ["http://localhost", "http://localhost:8000", "http://localhost:3000","http://localhost:7860"]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# Initialize embeddings and load FAISS index
def load_faiss_index():
    try:
        embeddings = OllamaEmbeddings(base_url="http://ollama:11434", model="nomic-embed-text")
        vector = FAISS.load_local("./nomic_embed", embeddings, allow_dangerous_deserialization=True)
        retriever = vector.as_retriever(search_kwargs={"k": 4})
        return retriever
    except Exception as e:
        raise RuntimeError(f"Failed to load FAISS index or initialize embeddings: {e}")

retriever = load_faiss_index()

# Initialize NVIDIA model
nvidia_api_key = os.getenv("NVIDIA_API_KEY")
if not nvidia_api_key:
    raise ValueError("NVIDIA_API_KEY not found in environment variables")

nvidia_llm = ChatNVIDIA(
    model="meta/llama-3.1-70b-instruct",
    api_key=nvidia_api_key,
    temperature=0.2,
    top_p=0.7,
    max_tokens=1024
)

def get_llm(provider, model):
    if provider.lower() == "ollama":
        return Ollama(base_url="http://ollama:11434", model="llama3.1")
    elif provider.lower() == "nvidia":
        return nvidia_llm
    else:
        raise ValueError(f"Invalid provider: {provider}")

# Initialize SentenceTransformer model for semantic similarity
model = SentenceTransformer('paraphrase-MiniLM-L6-v2')


# HYDE template and chain setup
hyde_template = """
Generate a detailed, realistic, and contextually appropriate hypothetical response to the following question as it would be handled in a sophisticated healthcare document management system.

{question}

Example:
Q: How does the document management system handle user authentication?
A: In a healthcare document management system, user authentication is typically handled through a multi-layered approach that includes secure passwords, biometric verification, and two-factor authentication (2FA). This ensures that only authorized users can access sensitive medical records, reducing the risk of data breaches.

Make sure to include plausible system functionalities, industry-standard practices, and relevant details.
"""

hyde_prompt = ChatPromptTemplate.from_template(hyde_template)
def get_hyde_query_transformer(provider, model):
    return hyde_prompt | get_llm(provider, model) | StrOutputParser()

def hyde_retriever(question, provider, model):
    hyde_query_transformer = get_hyde_query_transformer(provider, model)
    hypothetical_document = hyde_query_transformer.invoke({"question": question})
    return retriever.get_relevant_documents(hypothetical_document)

template = """
You are an AI assistant for the Healthcare Chatbot Q&A System. Provide a detailed and accurate response based solely on the given context.

Context:
{context}

Question: {question}

Instructions:
1. Start your response with "🤖 " followed by a concise introduction that summarizes the purpose or key aspect of the topic.
2. Provide a comprehensive list of features or functionalities, each as a detailed item.
3. Use bullet points under each item for additional details or explanations.
4. Only include information present in the given context. Avoid speculating or adding information not provided.
5. Refer to the system as 'Healthcare Document Retrieval' where appropriate.
6. Ensure the response maintains continuity and connection with any previous answers, linking them where applicable.
7. Aim for a complete, detailed, and contextually accurate response within the token limit.

Answer:
"""


prompt = ChatPromptTemplate.from_template(template)

def get_qa_chain(llm, retriever):
    # Define the HYDE chain
    hyde_chain = RunnablePassthrough.assign(
        hyde_query=lambda x: llm.invoke(hyde_prompt.format_messages(question=x["question"]))
    )

    # Define the retrieval chain
    retrieval_chain = RunnablePassthrough.assign(
        docs=lambda x: retriever.invoke(x["hyde_query"])
    )

    # Define the main QA chain
    qa_chain = RunnablePassthrough.assign(
        answer=lambda x: llm.invoke(prompt.format_messages(context=format_docs(x["docs"]), question=x["question"]))
    )

    # Combine the chains
    final_chain = hyde_chain | retrieval_chain | qa_chain

    return final_chain

def format_docs(docs):
    return "\n\n".join([
        f"Document {i+1}:\nTitle: {doc.metadata.get('source', 'Unknown')}\n"
        f"Content: {doc.page_content.replace('{{% system-name %}}', 'Healthcare System')}"
        for i, doc in enumerate(docs)
    ])

class DocumentMetadata(BaseModel):
    source: str

class Document(BaseModel):
    content: str
    metadata: DocumentMetadata

class EvaluationMetrics(BaseModel):
    semantic_similarity: float
    response_length: int

class GenerateResponse(BaseModel):
    answer: str
    documents: List[Document]
    evaluation: Optional[EvaluationMetrics] = None

class EmbeddingRequest(BaseModel):
    text: str

class EmbeddingResponse(BaseModel):
    embedding: List[float]

class EvaluationRequest(BaseModel):
    query: str
    response: str
    reference: Optional[str] = None
    provider: str
    model: str

class EvaluationResponse(BaseModel):
    semantic_similarity: float
    response_length: int
    reference_similarity: Optional[float] = None

class GenerateRequest(BaseModel):
    provider: str
    model: str
    prompt: str
    max_tokens: int = 100
    base_url: str = "http://ollama:11434"
    history: List[Dict[str, str]] = []  # To maintain chat-like history
    
    

# Add this new function to calculate semantic similarity
def semantic_similarity(text1, text2):
    embeddings = model.encode([text1, text2])
    return cosine_similarity([embeddings[0]], [embeddings[1]])[0][0]


def call_ollama_api(endpoint: str, payload: dict):
    url = f"http://ollama:11434{endpoint}"
    try:
        response = requests.post(url, json=payload, stream=True)
        response.raise_for_status()
        
        # If the response is streamed, handle it
        result = []
        for line in response.iter_lines():
            if line:
                try:
                    json_data = json.loads(line.decode('utf-8'))
                    result.append(json_data)
                except json.JSONDecodeError as e:
                    logging.error(f"JSON decoding error: {str(e)}")
                    raise HTTPException(status_code=500, detail=f"JSON decoding error: {str(e)}")

        return result
    except requests.RequestException as e:
        logging.error(f"Ollama API request failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Ollama API error: {str(e)}")



@app.post("/embeddings", response_model=EmbeddingResponse)
async def get_embeddings(request: EmbeddingRequest):
    try:
        embeddings = OllamaEmbeddings(base_url="http://ollama:11434", model="nomic-embed-text")
        result = embeddings.embed_query(request.text)
        return EmbeddingResponse(embedding=result)
    except Exception as e:
        logger.error(f"Error in get_embeddings: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest):
    try:
        llm = get_llm(request.provider, request.model)
        qa_chain = get_qa_chain(llm, retriever)
        
        logger.info(f"Invoking chain with question: {request.prompt}")
        
        result = qa_chain.invoke({"question": request.prompt})
        
        logger.info(f"Chain result: {result}")

        # Handle different result formats
        if isinstance(result['answer'], str):
            answer = result['answer']
        elif hasattr(result['answer'], 'content'):
            answer = result['answer'].content
        else:
            raise ValueError(f"Unexpected answer format: {type(result['answer'])}")

        retrieved_docs = result.get('docs', [])

        answer = answer.replace('{{% system-name %}}', 'Healthcare System')
                
        # Concise resource formatting
        resources = [doc.metadata.get('source', '').split('/')[-1].split('.')[0] for doc in retrieved_docs if doc.metadata.get('source')]
        formatted_resources = "📚 Related documents: " + ", ".join(resources)

        # Ensure the answer is complete before adding resources
        if not answer.strip().endswith('.'):
            answer = answer.strip() + '.'

        answer = f"{answer}\n\n{formatted_resources}"

        # Calculate evaluation metrics
        similarity = semantic_similarity(request.prompt, answer)
        response_length = len(answer.split())

        response = GenerateResponse(
            answer=answer,
            documents=[
                Document(
                    content=doc.page_content[:100],
                    source="/".join(doc.metadata.get('source', '').split('/')[-4:])  # Retrieve the last four segments of the path
                )
                for doc in retrieved_docs[:3]
            ],
            evaluation=EvaluationMetrics(
                semantic_similarity=similarity,
                response_length=response_length
            )
        )

        # Log the full response for debugging
        logger.info(f"Full response: {response.dict()}")

        return response
    
    except Exception as e:
        logger.error(f"Error in generate: {str(e)}", exc_info=True)
        error_message = str(e)
        if "Connection refused" in error_message:
            error_message = "Failed to connect to Ollama. Please ensure Ollama is running."
        elif "Unauthorized" in error_message:
            error_message = "NVIDIA API authentication failed. Please check your API key."
        raise HTTPException(status_code=500, detail=error_message)
    
    
# Memory class to keep track of important facts and points during the conversation
class ConversationMemory:
    def __init__(self, max_memory_size=5):
        self.memory = deque(maxlen=max_memory_size)

    def add_memory(self, message):
        self.memory.append(message)

    def get_memory(self):
        return "\n".join(self.memory)

    def clear_memory(self):
        self.memory.clear()
        
        
@app.post("/update_vector_db")
async def update_vector_db():
    try:
        # Call the update script
        logger.info("Starting vector database update...")
        result = subprocess.run(
            ["/app/update_vector_db.sh"], 
            capture_output=True, 
            text=True, 
            check=True
        )
        logger.info(f"Script output: {result.stdout}")
        return {"message": "Vector database updated successfully", "details": result.stdout}
    
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to update vector database: {e.stderr}")
        raise HTTPException(status_code=500, detail=f"Failed to update vector database: {e.stderr}")
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


# Initialize SentenceTransformer model for semantic similarity
model = SentenceTransformer('paraphrase-MiniLM-L6-v2')



# Add this new evaluation endpoint
@app.post("/evaluate", response_model=EvaluationResponse)
async def evaluate_response(request: EvaluationRequest):
    try:
        evaluation = EvaluationResponse(
            semantic_similarity=semantic_similarity(request.query, request.response),
            response_length=len(request.response.split())
        )
        
        if request.reference:
            evaluation.reference_similarity = semantic_similarity(request.response, request.reference)
        
        return evaluation
    except Exception as e:
        logger.error(f"Error in evaluate_response: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# Add this new endpoint to compare both LLMs
@app.post("/compare_llms")
async def compare_llms(request: GenerateRequest):
    try:
        results = {}
        for provider in ["ollama", "nvidia"]:
            req = GenerateRequest(
                provider=provider,
                model=request.model if provider == "ollama" else "meta/llama-3.1-70b-instruct",
                prompt=request.prompt,
                max_tokens=request.max_tokens
            )
            results[provider] = await generate(req)
        return results
    except Exception as e:
        logger.error(f"Error in compare_llms: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    return {"message": "Welcome to the Chatbot of Healthcare Q&A System"}

@app.get("/health")
async def health_check():
    return {"status": "ok"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the FastAPI server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the server on")
    args = parser.parse_args()
    
    uvicorn.run(app, host="0.0.0.0", port=args.port)
    
