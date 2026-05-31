#!/usr/bin/env python3
"""Environment diagnostic script - run this in your container"""

import sys
import subprocess
import os

print("=" * 60)
print("ENVIRONMENT DIAGNOSTICS")
print("=" * 60)

print(f"\n1. Python Version: {sys.version}")
print(f"2. Python Executable: {sys.executable}")
print(f"3. Working Directory: {os.getcwd()}")

print(f"\n4. sys.path:")
for p in sys.path:
    print(f"   - {p}")

print(f"\n5. Environment Variables:")
for key in ['PORT', 'GROQ_API_KEY', 'DEEPGRAM_API_KEY', 'PYTHONPATH']:
    val = os.getenv(key)
    print(f"   {key}: {'SET' if val else 'NOT SET'}")

print(f"\n6. Installed Packages:")
try:
    result = subprocess.run([sys.executable, "-m", "pip", "list"], 
                          capture_output=True, text=True)
    print(result.stdout)
except Exception as e:
    print(f"   ERROR: {e}")

print(f"\n7. Checking critical imports:")
packages = ['fastapi', 'uvicorn', 'groq', 'python-dotenv', 'websockets']
for pkg in packages:
    try:
        if pkg == 'python-dotenv':
            __import__('dotenv')
        else:
            __import__(pkg)
        print(f"   ✓ {pkg}: OK")
    except ImportError as e:
        print(f"   ✗ {pkg}: FAILED - {e}")

print(f"\n8. Files in current directory:")
try:
    files = os.listdir('.')
    for f in ['main.py', 'requirements.txt', 'Procfile', 'runtime.txt', '.env']:
        status = '✓' if f in files else '✗'
        print(f"   {status} {f}")
except Exception as e:
    print(f"   ERROR: {e}")

print("\n" + "=" * 60)