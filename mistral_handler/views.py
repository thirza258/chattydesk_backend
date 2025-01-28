from django.shortcuts import render
from mistralai import Mistral
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
import os
import json
from gpt_handler.models import HistoryPrompt

# Create your views here.
mistral = Mistral(api_key=os.getenv("MISTRAL_API_KEY"))

def create_response(message):
    model = "mistral-large-latest"
    
    chat_response = mistral.chat.complete(
        model = model,
        messages = [
            {
                "role": "user",
                "content": f"""
                You are tasked with formatting a given prompt using Markdown and encapsulating the response in JSON format. Follow these steps:

                1. Take the provided prompt and format it using Markdown syntax. This may include:
                - Adding headers with '#' symbols
                - Creating bold text with '**' or '__'
                - Adding italic text with '*' or '_'
                - Creating lists with '-' or '1.'
                - Adding code blocks with '```'
                - Creating links with '[text](URL)'
                - And any other relevant Markdown formatting

                2. After formatting the prompt, encapsulate the entire response in JSON format using the following structure:
                ```json
                {{
                    "response": "Your Markdown-formatted prompt goes here"
                }}
                ```

                3. Here is the prompt you need to format:
                <prompt>
                {message}
                </prompt>

                4. Make sure to escape any special characters in the JSON string as needed (e.g., newlines, quotation marks).

                5. Your final output should look similar to this example:
                ```json
                {{
                    "response": "# Title\n\n**Bold text** and *italic text*\n\n- List item 1\n- List item 2\n\n```\ncode block\n```\n\n[Link](https://example.com)"
                }}
                ```

                Remember to use appropriate Markdown formatting to enhance the readability and structure of the prompt. Ensure that the JSON format is valid and properly escaped.
                """
            }
        ]
    )
    
    response = chat_response.choices[0].message.content

    try:
        if response.startswith("```json") and response.endswith("```"):
            response = response[7:-3].strip()  # Remove ```json and ``` delimiters

        # Parse the cleaned response as JSON
        response_json = json.loads(response)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse AI response as JSON: {e}")

    return response_json

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

            
            history_prompt = HistoryPrompt(
                prompt=message,
                response=response,
                model_name="Mistral"
            )
            history_prompt.save()
            
            return Response({
                "status": 200,
                "message": "Success",
                "data": response
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": 500,
                "message": f"Internal Server Error: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)