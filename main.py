import os
import asyncio
import json
import time
import io
import wave
import math
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
import dotenv
import logging
import base64

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise ValueError("MISSING GROQ API KEY")

groq_client = Groq(api_key=groq_api_key)

SYSTEM_PROMPT = """You are an expert interview assistant. Give concise 3-bullet answers."""

TECH_CONTEXT = "Software engineering interview about React, JavaScript, TypeScript, Node.js, Python, system design, algorithms, data structures, coding problems."

conversations = {}
last_request_time = 0
MIN_REQUEST_INTERVAL = 1.5

def create_wav_from_pcm(pcm_data: bytes, sample_rate=16000):
    """Convert raw PCM int16 data to WAV format"""
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_data)
    return wav.getvalue()

def has_speech(audio_data: bytes, threshold=300):
    """Check if audio contains actual speech"""
    if len(audio_data) < 100:
        return False
    
    samples = []
    for i in range(0, len(audio_data), 2):
        if i+1 < len(audio_data):
            sample = int.from_bytes(audio_data[i:i+2], 'little', signed=True)
            samples.append(abs(sample))
    
    if not samples:
        return False
    
    avg_energy = sum(samples) / len(samples)
    max_energy = max(samples) if samples else 0
    
    logger.info(f"Audio energy: avg={avg_energy:.0f}, max={max_energy}, samples={len(samples)}")
    
    return avg_energy > threshold and max_energy > 1000

async def transcribe_with_retry(audio_data: bytes, max_retries=3):
    """Transcribe with better error handling"""
    global last_request_time
    
    wav_data = create_wav_from_pcm(audio_data)
    logger.info(f"WAV size: {len(wav_data)} bytes")
    
    for attempt in range(max_retries):
        time_since_last = time.time() - last_request_time
        if time_since_last < MIN_REQUEST_INTERVAL:
            await asyncio.sleep(MIN_REQUEST_INTERVAL - time_since_last)
        
        try:
            last_request_time = time.time()
            
            result = groq_client.audio.transcriptions.create(
                file=("audio.wav", wav_data, "audio/wav"),
                model="whisper-large-v3-turbo",
                language="en",
                prompt=TECH_CONTEXT,
                response_format="text",
                temperature=0.0,
            )
            
            text = str(result).strip()
            logger.info(f"Transcription: '{text}'")
            return text
            
        except Exception as e:
            logger.error(f"Transcription error (attempt {attempt+1}): {e}")
            if "429" in str(e) or "rate limit" in str(e).lower():
                wait_time = 2 ** attempt
                logger.warning(f"Rate limited, waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise
    
    return ""

async def generate_answer(question: str, history: list):
    """Generate answer with better prompt"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    for h in history[-2:]:
        messages.append({"role": "user", "content": h['question']})
        messages.append({"role": "assistant", "content": h['answer']})
    
    messages.append({"role": "user", "content": f"Interview question: {question}"})
    
    global last_request_time
    time_since_last = time.time() - last_request_time
    if time_since_last < 0.5:
        await asyncio.sleep(0.5 - time_since_last)
    
    last_request_time = time.time()
    
    r = groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.1-8b-instant",
        temperature=0.3,
        max_tokens=150,
    )
    return r.choices[0].message.content

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = id(websocket)
    logger.info(f"Client {client_id} connected")
    
    conversations[client_id] = []
    buffer = bytearray()
    last_text = ""
    
    BUFFER_SIZE = 64000  # 2 seconds of audio
    
    try:
        while True:
            # Receive text message (JSON)
            message = await websocket.receive_text()
            logger.info(f"Received text: {message[:100]}...")
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.error("Invalid JSON")
                continue
            
            # Handle audio message
            if data.get('type') == 'AUDIO':
                # Decode base64 to binary
                try:
                    audio_bytes = base64.b64decode(data['data'])
                    buffer.extend(audio_bytes)
                    logger.info(f"Buffer: {len(buffer)}/{BUFFER_SIZE}")
                except Exception as e:
                    logger.error(f"Decode error: {e}")
                    continue
                
                # Process when buffer is full
                if len(buffer) >= BUFFER_SIZE:
                    process_data = bytes(buffer[:BUFFER_SIZE])
                    buffer = buffer[-8000:]  # Keep 0.5s overlap
                    
                    logger.info(f"Processing {len(process_data)} bytes")
                    
                    # Check for speech
                    if not has_speech(process_data):
                        logger.info("Silence detected")
                        continue
                    
                    # Transcribe
                    try:
                        text = await transcribe_with_retry(process_data)
                        
                        if len(text) < 3:
                            continue
                        
                        # Filter noise
                        noise_phrases = ["thank", "thanks", "okay", "um", "uh", "hm", "mm"]
                        text_lower = text.lower()
                        if any(p in text_lower for p in noise_phrases) and len(text) < 20:
                            continue
                        
                        if last_text and text_lower == last_text.lower():
                            continue
                        
                        logger.info(f"✓ Heard: {text}")
                        
                        # Generate answer
                        answer = await generate_answer(text, conversations[client_id])
                        
                        # Store and send
                        conversations[client_id].append({
                            "question": text,
                            "answer": answer,
                            "time": time.time()
                        })
                        
                        await websocket.send_text(f"Q:{text}")
                        await websocket.send_text(f"A:{answer}")
                        last_text = text
                        
                    except Exception as e:
                        logger.error(f"Processing error: {e}")
            
            elif data.get('type') == 'ping':
                await websocket.send_text('pong')
    
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        if client_id in conversations:
            del conversations[client_id]
        logger.info(f"Client {client_id} cleanup complete")