from django.shortcuts import render
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
import os
import json
from gpt_handler.models import HistoryPrompt
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def create_response(message) :
    response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {
        "role": "system",
        "content": [
            {
            "type": "text",
            "text": "You are a helpful assistant that will answer with Markdown format"
            }
        ]
        },
        {
        "role": "assistant",
        "content": [
            {
            "type": "text",
            "text": "{\"response\":\"Hello! How can I assist you today?\"}"
            }
        ]
        },
        {
        "role": "user",
        "content": [
            {
            "type": "text",
            "text": "what is realtime websocket api"
            }
        ]
        },
        {
        "role": "assistant",
        "content": [
            {
            "type": "text",
            "text": "{\"response\":\"A **real-time WebSocket API** is an application programming interface that allows for two-way interactive communication sessions between a user's browser and a server. This connection is established over a WebSocket protocol, which provides full-duplex channels over a single TCP connection.\\n\\n### Key Features of Real-time WebSocket API:\\n\\n1. **Bi-directional Communication**: Unlike HTTP, which is request/response, WebSockets allow for two-way communication without the overhead of sending HTTP headers.\\n\\n2. **Real-time Data Transfer**: WebSockets are used for real-time applications such as chat applications, live sports updates, stock tickers, multiplayer games, etc., where low latency is critical.\\n\\n3. **Persistent Connection**: Once a WebSocket connection is established, it stays open until the client or server decides to close it. This is efficient for applications that require constant data flow.\\n\\n4. **Less Latency**: Comparatively lower latency as WebSockets avoid the overhead of multiple HTTP requests.\\n\\n5. **Scalability**: Supports many concurrent connections with less resource usage.\\n\\n### How It Works:\\n- **WebSocket Handshake**: The interaction begins with the client sending a WebSocket handshake request. This is an HTTP-based request that transforms the HTTP connection to a WebSocket connection.\\n\\n- **Full-Duplex Communication**: Post-handshake, both the client and server can send messages to each other at any time, enabling real-time data flow.\\n\\n- **Closing the Connection**: Either the client or the server can close the connection, though the connection is often kept open as long as needed to minimize the cost of opening new connections.\\n\\n### Use Cases:\\n- **Chat Applications**: Where messages need to be delivered instantly.\\n- **Live Updates/Notifications**: Stock prices, sports scores.\\n- **Collaborative Online Tools**: Real-time editing tools.\\n- **Online Gaming**: Multiplayer games where real-time interactions between clients are essential.\\n\\n### Example Technology Stack:\\n- **Client**: Web browsers or any front-end framework that supports WebSockets.\\n- **Server**: Technologies like Node.js, Python (using libraries like Tornado, Flask-SocketIO), or Java (Spring) can be used to build the server-side of WebSocket.\\n\\n### Conclusion:\\nWebSockets provide a way to open an interactive communication session between the user's browser and a server, facilitating real-time web applications that require constant data flow and low latency.\"}"
            }
        ]
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": message
                }
            ]
        }
    ],
    response_format={
        "type": "json_schema",
        "json_schema": {
        "name": "response_schema",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
            "response": {
                "type": "string",
                "description": "The response message."
            }
            },
            "required": [
            "response"
            ],
            "additionalProperties": False
        }
        }
    },
    temperature=1,
    max_completion_tokens=2048,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0
    )
    
    return response.choices[0].message.content
    
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

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
            
            if isinstance(response, str):
                response_data = json.loads(response)
            else:
                response_data = response
                
            history_prompt = HistoryPrompt.objects.create(
                prompt=message,
                response=response_data.get('response'),
                model_name="ChatGPT"
            )
            history_prompt.save()

            return Response({
                "status": 200,
                "message": "Success",
                "data": response_data
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": 500,
                "message": f"Internal Server Error: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
class GetHistoryPrompt(APIView):
    def get(self, request):
        history_prompts = HistoryPrompt.objects.all().order_by('-created_at')
        data = []
        
        for history_prompt in history_prompts:
            data.append({
                "prompt": history_prompt.prompt,
                "response": history_prompt.response,
                "model_name": history_prompt.model_name,
                "created_at": history_prompt.created_at
            })
        
        return Response({
            "status": 200,
            "message": "Success",
            "data": data
        }, status=status.HTTP_200_OK)
        
    