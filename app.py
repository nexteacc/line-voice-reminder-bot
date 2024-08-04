import os
import tempfile
from datetime import datetime
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, AudioMessage, TextSendMessage
import groq
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# LINE Bot configuration
line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# Groq API configuration
groq_client = groq.Groq(api_key=os.getenv('GROQ_API_KEY'))

# Database configuration
engine = create_engine('sqlite:///reminders.db', echo=True)
Base = declarative_base()
Session = sessionmaker(bind=engine)

# Define Reminder model
class Reminder(Base):
    __tablename__ = 'reminders'

    id = Column(Integer, primary_key=True)
    user_id = Column(String)
    event_time = Column(DateTime)
    event_content = Column(String)
    is_sent = Column(Boolean, default=False)

# Create tables
Base.metadata.create_all(engine)

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    
    # Get audio content
    message_content = line_bot_api.get_message_content(message_id)
    
    # Save audio content to a temporary file
    with tempfile.NamedTemporaryFile(delete=False, suffix='.m4a') as temp_audio:
        for chunk in message_content.iter_content():
            temp_audio.write(chunk)
        temp_audio_path = temp_audio.name
    
    # Transcribe audio using Groq Whisper API
    with open(temp_audio_path, "rb") as audio_file:
        transcription = groq_client.audio.transcriptions.create(
            file=(temp_audio_path, audio_file),
            model="whisper-large-v3",
            response_format="json"
        )
    
    # Extract event information using Groq LLM
    prompt = f"Extract the event time and content from this text: {transcription.text}. Format the response as two lines: first line is the event time in 'HH:MM YYYY-MM-DD' format, second line is the event content."
    completion = groq_client.chat.completions.create(
        messages=[
            {"role": "system", "content": "You are a helpful assistant that extracts event information."},
            {"role": "user", "content": prompt}
        ],
        model="llama3-8b-8192",
        max_tokens=200
    )
    
    extracted_info = completion.choices[0].message.content.strip().split('\n')
    if len(extracted_info) == 2:
        event_time_str, event_content = extracted_info
        event_time = datetime.strptime(event_time_str, "%H:%M %Y-%m-%d")
        
        # Save reminder to database
        session = Session()
        new_reminder = Reminder(user_id=user_id, event_time=event_time, event_content=event_content)
        session.add(new_reminder)
        session.commit()
        
        # Schedule reminder
        scheduler.add_job(send_reminder, 'date', run_date=event_time, args=[user_id, event_time, event_content])
        
        # Send confirmation to user
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"Reminder set for {event_time_str}: {event_content}")
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Sorry, I couldn't understand the event details. Please try again.")
        )
    
    # Clean up temporary file
    os.unlink(temp_audio_path)

def send_reminder(user_id, event_time, event_content):
    line_bot_api.push_message(
        user_id,
        TextSendMessage(text=f"Reminder: {event_time.strftime('%H:%M %Y-%m-%d')} - {event_content}")
    )
    
    # Mark reminder as sent in database
    session = Session()
    reminder = session.query(Reminder).filter_by(user_id=user_id, event_time=event_time, event_content=event_content, is_sent=False).first()
    if reminder:
        reminder.is_sent = True
        session.commit()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)