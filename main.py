import os
import asyncio
import json
import time
import io
import wave
import math
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware  # ADD THIS IMPORT
from groq import Groq
import dotenv
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()

app = FastAPI()

# ========== ADD CORS MIDDLEWARE HERE ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins (change to specific domain in production)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ==============================================

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise ValueError("MISSING GROQ API KEY")

groq_client = Groq(api_key=groq_api_key)

SYSTEM_PROMPT = """You are an expert interview assistant. Give concise 3-bullet answers."""

# Technical context for better recognition
TECH_CONTEXT = "Software engineering interview: React, JavaScript, hooks, useState, useEffect, components, props, state, API, frontend, backend, Node.js."

conversations = {}
last_request_time = 0
MIN_REQUEST_INTERVAL = 1.5  # Seconds between API calls to avoid rate limits

def create_wav(audio_data: bytes):
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio_data)
    return wav.getvalue()

def has_speech(audio_data: bytes, threshold=500):
    """Check if audio contains actual speech (not silence)"""
    if len(audio_data) < 100:
        return False
    # Simple energy check
    samples = []
    for i in range(0, len(audio_data), 2):
        if i+1 < len(audio_data):
            sample = int.from_bytes(audio_data[i:i+2], 'little', signed=True)
            samples.append(abs(sample))
    if not samples:
        return False
    avg_energy = sum(samples) / len(samples)
    return avg_energy > threshold

async def transcribe_with_retry(audio_data: bytes, max_retries=3):
    """Transcribe with rate limit handling"""
    global last_request_time
    
    wav = create_wav(audio_data)
    
    for attempt in range(max_retries):
        # Rate limit delay
        time_since_last = time.time() - last_request_time
        if time_since_last < MIN_REQUEST_INTERVAL:
            await asyncio.sleep(MIN_REQUEST_INTERVAL - time_since_last)
        
        try:
            last_request_time = time.time()
            result = groq_client.audio.transcriptions.create(
                file=("audio.wav", wav, "audio/wav"),
                model="whisper-large-v3-turbo",
                language="en",
                prompt=TECH_CONTEXT,
                response_format="text"
            )
            return str(result).strip()
            
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                wait_time = 2 ** attempt  # Exponential backoff: 1, 2, 4 seconds
                logger.warning(f"Rate limited, waiting {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                raise
    
    return ""

async def generate_answer(question: str, history: list):
    """Generate answer"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    for h in history[-2:]:
        messages.append({"role": "user", "content": h['question']})
        messages.append({"role": "assistant", "content": h['answer']})
    
    messages.append({"role": "user", "content": question})
    
    # Rate limit
    global last_request_time
    time_since_last = time.time() - last_request_time
    if time_since_last < 0.5:
        await asyncio.sleep(0.5 - time_since_last)
    
    last_request_time = time.time()
    
    r = groq_client.chat.completions.create(
        messages=messages,
        model="llama-3.1-8b-instant",
        temperature=0.5,
        max_tokens=80,
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
    last_process_time = 0
    
    # 4 second buffer for better accuracy
    BUFFER_SIZE = 16000 * 4 * 2
    
    try:
        while True:
            data = await websocket.receive_bytes()
            buffer.extend(data)
            
            # Process every 4 seconds
            if len(buffer) >= BUFFER_SIZE:
                # Check if enough time passed (rate limiting)
                if time.time() - last_process_time < 1.0:
                    # Keep only last 1 second for overlap
                    buffer = buffer[-16000*2:]
                    continue
                
                process_data = bytes(buffer[:BUFFER_SIZE])
                buffer = buffer[-16000:]  # Keep 0.5s overlap
                
                # Check for speech (skip silence)
                if not has_speech(process_data):
                    logger.info("Silence detected, skipping")
                    last_process_time = time.time()
                    continue
                
                try:
                    last_process_time = time.time()
                    start = time.time()
                    text = await transcribe_with_retry(process_data)
                    transcribe_time = time.time() - start
                    
                    if len(text) < 5:  # Skip very short
                        continue
                    
                    # Skip common noise phrases
                    noise_phrases = ["thank you", "thanks", "okay", "um", "uh", "hm", "mm"]
                    if any(p in text.lower() and len(text) < 20 for p in noise_phrases):
                        logger.info(f"Noise phrase skipped: {text}")
                        continue
                    
                    # Better duplicate detection (fuzzy match)
                    text_lower = text.lower()
                    if last_text and (text_lower in last_text or last_text in text_lower):
                        similarity = len(set(text_lower.split()) & set(last_text.split())) / max(len(text_lower.split()), 1)
                        if similarity > 0.6:
                            logger.info(f"Similar to last ({similarity:.2f}), skipping")
                            continue
                    
                    logger.info(f"✓ Heard ({transcribe_time:.1f}s): {text}")
                    
                    # Generate answer
                    start = time.time()
                    answer = await generate_answer(text, conversations[client_id])
                    gen_time = time.time() - start
                    
                    # Store
                    conversations[client_id].append({
                        "question": text,
                        "answer": answer,
                        "time": time.time()
                    })
                    
                    # Send
                    await websocket.send_text(f"Q:{text}")
                    await websocket.send_text(f"A:{answer}")
                    last_text = text
                    
                except Exception as e:
                    logger.error(f"Error processing audio: {e}")
                    await websocket.send_text(f"ERROR:{str(e)}")
    
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
        if client_id in conversations:
            del conversations[client_id]
                   