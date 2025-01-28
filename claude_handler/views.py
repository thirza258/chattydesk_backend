from django.shortcuts import render
import anthropic
import os
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from gpt_handler.models import HistoryPrompt

client = anthropic.Anthropic(
    # defaults to os.environ.get("ANTHROPIC_API_KEY")
    api_key= os.getenv("CLAUDE_API_KEY"),
)

def create_response(message):
    # Replace placeholders like {{PROMPT}} with real values,
    # because the SDK does not support variables.
    response = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=4096,
        temperature=0,
        system="You are tasked with answering a given prompt in Markdown format. Follow these instructions carefully:\n\n1. Read the following prompt:\n<prompt>\n{{PROMPT}}\n</prompt>\n\n2. Compose your response to the prompt using Markdown formatting. Use appropriate Markdown syntax for headings, lists, emphasis, links, and any other relevant formatting elements.\n\n3. After composing your response, wrap it in a JSON object with a single key \"response\" whose value is your Markdown-formatted answer.\n\n4. Ensure that your JSON is properly formatted and that the Markdown within it is correctly escaped.\n\nHere's an example of how your output should be structured:\n\n```json\n{\n  \"response\": \"# Main Heading\\n\\n## Subheading\\n\\nThis is a paragraph with **bold** and *italic* text.\\n\\n- List item 1\\n- List item 2\\n\\n[Link text](https://example.com)\"\n}\n```\n\nRemember to replace the example content with your actual response to the given prompt, maintaining proper Markdown formatting and JSON structure.\n\nProvide your response to the prompt now, following the instructions and format described above.",
        messages=[{
            "role": "user",
            "content": f"{{PROMPT}}{message}{{/PROMPT}}"
        }]
    )

    # Check if response.content is a list of TextBlock objects
    if isinstance(response.content, list) and len(response.content) > 0:
        # Access the text attribute of the first TextBlock object
        response_text = response.content[0].text
    else:
        response_text = response.content

    # Clean up response_text by replacing control characters (e.g., newlines) or unescaped quotes
    # Remove potential newlines or carriage returns that might cause parsing issues
    response_text = response_text.replace("\n", " ").replace("\r", "").strip()

    # Ensure response_text is a valid JSON string and try parsing
    try:
        response_json = json.loads(response_text)
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
                model_name="Claude"
            )
            history_prompt.save()
            
            return Response({
                "status": 200,
                "message": "Success",
                "data": response  # This now includes the response as requested
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({
                "status": 500,
                "message": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

