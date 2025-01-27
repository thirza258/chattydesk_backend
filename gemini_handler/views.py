from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import json
import os
import google.generativeai as genai
from google.ai.generativelanguage_v1beta.types import content
from gpt_handler.models import HistoryPrompt

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def create_response(message):
    generation_config = {
    "temperature": 1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
    "response_schema": content.Schema(
        type = content.Type.OBJECT,
        properties = {
        "response": content.Schema(
            type = content.Type.STRING,
        ),
        },
    ),
    "response_mime_type": "application/json",
    }

    model = genai.GenerativeModel(
    model_name="gemini-2.0-flash-exp",
    generation_config=generation_config,
    system_instruction="Answer in markdown format so it's like response : response in Markdown format",
    )

    chat_session = model.start_chat(
    history=[
        {
        "role": "user",
        "parts": [
            "What is RAG",
        ],
        },
        {
        "role": "model",
        "parts": [
            "```json\n{\n  \"response\": \"RAG stands for Retrieval-Augmented Generation. It's a technique that combines the power of information retrieval with the creative abilities of language models. Instead of relying solely on its pre-trained knowledge, a RAG model first retrieves relevant information from an external source (like a database or the web) and then uses that information to generate a more accurate and contextually appropriate response.\"\n}\n```",
        ],
        },
        {
        "role": "user",
        "parts": [
            "Hello\n",
        ],
        },
        {
        "role": "model",
        "parts": [
            "```json\n{\n  \"response\": \"Hello there!\"\n}\n```",
        ],
        },
    ]
    )
    response = chat_session.send_message(message)
    return response.text
# Create your views here.

class GenerateChat(APIView):
    def post(self, request):
        message = request.data.get('message')
        
        if not message:
            return Response({
                "status": 400,
                "message": "Bad Request: 'message' field is required."
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            response = create_response(message)
            
            try:
                response = json.loads(response)
                
                history_prompt = HistoryPrompt(
                    prompt=message,
                    response=response["response"],
                    model_name="gemini"
                )
                history_prompt.save()
            except Exception as e:
                return Response({
                    "status": 500,
                    "message": str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
            return Response({
                "status": 200,
                "message": "Success",
                "data": response
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": 500,
                "message": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
