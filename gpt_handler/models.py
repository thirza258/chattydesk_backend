from django.db import models

# Create your models here.
class HistoryPrompt(models.Model):
    prompt = models.TextField()
    response = models.TextField()
    conversation_id = models.CharField(max_length=100, blank=True, null=True)
    model_name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.prompt
