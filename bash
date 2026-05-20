curl -X POST \
  'https://graph.facebook.com/v18.0/1134185349777146/messages' \
  -H "Authorization: Bearer VOTRE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "messaging_product": "whatsapp",
    "recipient_type": "individual",
    "to": "22558337112",
    "type": "text",
    "text": {"body": "Test direct"}
  }'
