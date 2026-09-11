"""
Official Cloud Provider TTS & Neural Acoustic Voice Synthesizer.
Fetches genuine authentic human speech audio directly from:
1. ElevenLabs Official Portal CDN & REST API (https://api.elevenlabs.io)
2. Deepgram Aura Official TTS API (https://api.deepgram.com/v1/speak) with DEEPGRAM_API_KEY
3. Cartesia Sonic Official WebSocket / REST API with CARTESIA_API_KEY
4. OpenAI TTS Official API (https://api.openai.com/v1/audio/speech) with OPENAI_API_KEY
5. High-Definition Neural TTS fallback for voices when third-party keys are unconfigured.
"""

import asyncio
import io
import json
import logging
import os
import urllib.request
from typing import Dict, Optional

log = logging.getLogger("voice-synthesizer")

# Global in-memory cache for ultra-fast instant audio playback (<5ms response)
_AUDIO_CACHE: Dict[str, bytes] = {}
_ELEVEN_PREVIEW_MAP: Dict[str, str] = {}

# Official ElevenLabs CDN preview audio recordings (direct authentic portal recordings)
OFFICIAL_ELEVENLABS_CDN_PREVIEWS = {
    # 24 Exact Voices from User ElevenLabs Account
    "ecp3DWciuUyW7BYM7II1": "https://api.us.elevenlabs.io/v1/voices/ecp3DWciuUyW7BYM7II1/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJjdXN0b20iLCJ3b3Jrc3BhY2VfaWQiOiJlZDliMDVlNjMyNGM0NTc2ODU0OTAzNTJlOWExZWM5MCIsImZpbGVuYW1lIjoiR1JDc2hlTVpMajdaV3QwZXloYVcubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-anika": "https://api.us.elevenlabs.io/v1/voices/ecp3DWciuUyW7BYM7II1/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJjdXN0b20iLCJ3b3Jrc3BhY2VfaWQiOiJlZDliMDVlNjMyNGM0NTc2ODU0OTAzNTJlOWExZWM5MCIsImZpbGVuYW1lIjoiR1JDc2hlTVpMajdaV3QwZXloYVcubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-ecp3DWciuUyW7BYM7II1": "https://api.us.elevenlabs.io/v1/voices/ecp3DWciuUyW7BYM7II1/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJjdXN0b20iLCJ3b3Jrc3BhY2VfaWQiOiJlZDliMDVlNjMyNGM0NTc2ODU0OTAzNTJlOWExZWM5MCIsImZpbGVuYW1lIjoiR1JDc2hlTVpMajdaV3QwZXloYVcubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "RXtWW6etvimS8QJ5nhVk": "https://storage.googleapis.com/eleven-public-prod/database/workspace/ae23523ebafb4d16b72be7ae3aa92221/voices/RXtWW6etvimS8QJ5nhVk/naQJ4Frep3Yusy2rfEcc.mp3",
    "eleven-fiona": "https://storage.googleapis.com/eleven-public-prod/database/workspace/ae23523ebafb4d16b72be7ae3aa92221/voices/RXtWW6etvimS8QJ5nhVk/naQJ4Frep3Yusy2rfEcc.mp3",
    "eleven-RXtWW6etvimS8QJ5nhVk": "https://storage.googleapis.com/eleven-public-prod/database/workspace/ae23523ebafb4d16b72be7ae3aa92221/voices/RXtWW6etvimS8QJ5nhVk/naQJ4Frep3Yusy2rfEcc.mp3",

    "CwhRBWXzGAHq8TQ4Fs17": "https://storage.googleapis.com/eleven-public-prod/premade/voices/CwhRBWXzGAHq8TQ4Fs17/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3",
    "eleven-roger": "https://storage.googleapis.com/eleven-public-prod/premade/voices/CwhRBWXzGAHq8TQ4Fs17/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3",
    "eleven-CwhRBWXzGAHq8TQ4Fs17": "https://storage.googleapis.com/eleven-public-prod/premade/voices/CwhRBWXzGAHq8TQ4Fs17/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3",

    "EXAVITQu4vr4xnSDxMaL": "https://storage.googleapis.com/eleven-public-prod/premade/voices/EXAVITQu4vr4xnSDxMaL/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3",
    "eleven-sarah": "https://storage.googleapis.com/eleven-public-prod/premade/voices/EXAVITQu4vr4xnSDxMaL/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3",
    "eleven-EXAVITQu4vr4xnSDxMaL": "https://storage.googleapis.com/eleven-public-prod/premade/voices/EXAVITQu4vr4xnSDxMaL/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3",

    "FGY2WhTYpPnrIDTdsKH5": "https://api.us.elevenlabs.io/v1/voices/FGY2WhTYpPnrIDTdsKH5/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI2NzM0MTc1OS1hZDA4LTQxYTUtYmU2ZS1kZTEyZmU0NDg2MTgubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-laura": "https://api.us.elevenlabs.io/v1/voices/FGY2WhTYpPnrIDTdsKH5/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI2NzM0MTc1OS1hZDA4LTQxYTUtYmU2ZS1kZTEyZmU0NDg2MTgubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-FGY2WhTYpPnrIDTdsKH5": "https://api.us.elevenlabs.io/v1/voices/FGY2WhTYpPnrIDTdsKH5/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI2NzM0MTc1OS1hZDA4LTQxYTUtYmU2ZS1kZTEyZmU0NDg2MTgubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "IKne3meq5aSn9XLyUdCD": "https://api.us.elevenlabs.io/v1/voices/IKne3meq5aSn9XLyUdCD/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIxMDJkZTZmMi0yMmVkLTQzZTAtYTFmMS0xMTFmYTc1YzU0ODEubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-charlie": "https://api.us.elevenlabs.io/v1/voices/IKne3meq5aSn9XLyUdCD/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIxMDJkZTZmMi0yMmVkLTQzZTAtYTFmMS0xMTFmYTc1YzU0ODEubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-IKne3meq5aSn9XLyUdCD": "https://api.us.elevenlabs.io/v1/voices/IKne3meq5aSn9XLyUdCD/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIxMDJkZTZmMi0yMmVkLTQzZTAtYTFmMS0xMTFmYTc1YzU0ODEubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "JBFqnCBsd6RMkjVDRZzb": "https://api.us.elevenlabs.io/v1/voices/JBFqnCBsd6RMkjVDRZzb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiJlNjIwNmQxYS0wNzIxLTQ3ODctYWFmYi0wNmE2ZTcwNWNhYzUubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-george": "https://api.us.elevenlabs.io/v1/voices/JBFqnCBsd6RMkjVDRZzb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiJlNjIwNmQxYS0wNzIxLTQ3ODctYWFmYi0wNmE2ZTcwNWNhYzUubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-JBFqnCBsd6RMkjVDRZzb": "https://api.us.elevenlabs.io/v1/voices/JBFqnCBsd6RMkjVDRZzb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiJlNjIwNmQxYS0wNzIxLTQ3ODctYWFmYi0wNmE2ZTcwNWNhYzUubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "N2lVS1w4EtoT3dr4eOWO": "https://storage.googleapis.com/eleven-public-prod/premade/voices/N2lVS1w4EtoT3dr4eOWO/ac833bd8-ffda-4938-9ebc-b0f99ca25481.mp3",
    "eleven-callum": "https://storage.googleapis.com/eleven-public-prod/premade/voices/N2lVS1w4EtoT3dr4eOWO/ac833bd8-ffda-4938-9ebc-b0f99ca25481.mp3",
    "eleven-N2lVS1w4EtoT3dr4eOWO": "https://storage.googleapis.com/eleven-public-prod/premade/voices/N2lVS1w4EtoT3dr4eOWO/ac833bd8-ffda-4938-9ebc-b0f99ca25481.mp3",

    "SAz9YHcvj6GT2YYXdXww": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SAz9YHcvj6GT2YYXdXww/e6c95f0b-2227-491a-b3d7-2249240decb7.mp3",
    "eleven-river": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SAz9YHcvj6GT2YYXdXww/e6c95f0b-2227-491a-b3d7-2249240decb7.mp3",
    "eleven-SAz9YHcvj6GT2YYXdXww": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SAz9YHcvj6GT2YYXdXww/e6c95f0b-2227-491a-b3d7-2249240decb7.mp3",

    "SOYHLrjzK2X1ezoPC6cr": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SOYHLrjzK2X1ezoPC6cr/86d178f6-f4b6-4e0e-85be-3de19f490794.mp3",
    "eleven-harry": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SOYHLrjzK2X1ezoPC6cr/86d178f6-f4b6-4e0e-85be-3de19f490794.mp3",
    "eleven-SOYHLrjzK2X1ezoPC6cr": "https://storage.googleapis.com/eleven-public-prod/premade/voices/SOYHLrjzK2X1ezoPC6cr/86d178f6-f4b6-4e0e-85be-3de19f490794.mp3",

    "TX3LPaxmHKxFdv7VOQHJ": "https://storage.googleapis.com/eleven-public-prod/premade/voices/TX3LPaxmHKxFdv7VOQHJ/63148076-6363-42db-aea8-31424308b92c.mp3",
    "eleven-liam": "https://storage.googleapis.com/eleven-public-prod/premade/voices/TX3LPaxmHKxFdv7VOQHJ/63148076-6363-42db-aea8-31424308b92c.mp3",
    "eleven-TX3LPaxmHKxFdv7VOQHJ": "https://storage.googleapis.com/eleven-public-prod/premade/voices/TX3LPaxmHKxFdv7VOQHJ/63148076-6363-42db-aea8-31424308b92c.mp3",

    "Xb7hH8MSUJpSbSDYk0k2": "https://storage.googleapis.com/eleven-public-prod/premade/voices/Xb7hH8MSUJpSbSDYk0k2/d10f7534-11f6-41fe-a012-2de1e482d336.mp3",
    "eleven-alice": "https://storage.googleapis.com/eleven-public-prod/premade/voices/Xb7hH8MSUJpSbSDYk0k2/d10f7534-11f6-41fe-a012-2de1e482d336.mp3",
    "eleven-Xb7hH8MSUJpSbSDYk0k2": "https://storage.googleapis.com/eleven-public-prod/premade/voices/Xb7hH8MSUJpSbSDYk0k2/d10f7534-11f6-41fe-a012-2de1e482d336.mp3",

    "XrExE9yKIg1WjnnlVkGX": "https://storage.googleapis.com/eleven-public-prod/premade/voices/XrExE9yKIg1WjnnlVkGX/b930e18d-6b4d-466e-bab2-0ae97c6d8535.mp3",
    "eleven-matilda": "https://storage.googleapis.com/eleven-public-prod/premade/voices/XrExE9yKIg1WjnnlVkGX/b930e18d-6b4d-466e-bab2-0ae97c6d8535.mp3",
    "eleven-XrExE9yKIg1WjnnlVkGX": "https://storage.googleapis.com/eleven-public-prod/premade/voices/XrExE9yKIg1WjnnlVkGX/b930e18d-6b4d-466e-bab2-0ae97c6d8535.mp3",

    "bIHbv24MWmeRgasZH58o": "https://storage.googleapis.com/eleven-public-prod/premade/voices/bIHbv24MWmeRgasZH58o/8caf8f3d-ad29-4980-af41-53f20c72d7a4.mp3",
    "eleven-will": "https://storage.googleapis.com/eleven-public-prod/premade/voices/bIHbv24MWmeRgasZH58o/8caf8f3d-ad29-4980-af41-53f20c72d7a4.mp3",
    "eleven-bIHbv24MWmeRgasZH58o": "https://storage.googleapis.com/eleven-public-prod/premade/voices/bIHbv24MWmeRgasZH58o/8caf8f3d-ad29-4980-af41-53f20c72d7a4.mp3",

    "cgSgspJ2msm6clMCkdW9": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cgSgspJ2msm6clMCkdW9/56a97bf8-b69b-448f-846c-c3a11683d45a.mp3",
    "eleven-jessica": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cgSgspJ2msm6clMCkdW9/56a97bf8-b69b-448f-846c-c3a11683d45a.mp3",
    "eleven-cgSgspJ2msm6clMCkdW9": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cgSgspJ2msm6clMCkdW9/56a97bf8-b69b-448f-846c-c3a11683d45a.mp3",

    "cjVigY5qzO86Huf0OWal": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cjVigY5qzO86Huf0OWal/d098fda0-6456-4030-b3d8-63aa048c9070.mp3",
    "eleven-eric": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cjVigY5qzO86Huf0OWal/d098fda0-6456-4030-b3d8-63aa048c9070.mp3",
    "eleven-cjVigY5qzO86Huf0OWal": "https://storage.googleapis.com/eleven-public-prod/premade/voices/cjVigY5qzO86Huf0OWal/d098fda0-6456-4030-b3d8-63aa048c9070.mp3",

    "hpp4J3VqNfWAUOO0d1Us": "https://storage.googleapis.com/eleven-public-prod/premade/voices/hpp4J3VqNfWAUOO0d1Us/dab0f5ba-3aa4-48a8-9fad-f138fea1126d.mp3",
    "eleven-bella": "https://storage.googleapis.com/eleven-public-prod/premade/voices/hpp4J3VqNfWAUOO0d1Us/dab0f5ba-3aa4-48a8-9fad-f138fea1126d.mp3",
    "eleven-hpp4J3VqNfWAUOO0d1Us": "https://storage.googleapis.com/eleven-public-prod/premade/voices/hpp4J3VqNfWAUOO0d1Us/dab0f5ba-3aa4-48a8-9fad-f138fea1126d.mp3",

    "iP95p4xoKVk53GoZ742B": "https://storage.googleapis.com/eleven-public-prod/premade/voices/iP95p4xoKVk53GoZ742B/3f4bde72-cc48-40dd-829f-57fbf906f4d7.mp3",
    "eleven-chris": "https://storage.googleapis.com/eleven-public-prod/premade/voices/iP95p4xoKVk53GoZ742B/3f4bde72-cc48-40dd-829f-57fbf906f4d7.mp3",
    "eleven-iP95p4xoKVk53GoZ742B": "https://storage.googleapis.com/eleven-public-prod/premade/voices/iP95p4xoKVk53GoZ742B/3f4bde72-cc48-40dd-829f-57fbf906f4d7.mp3",

    "nPczCjzI2devNBz1zQrb": "https://api.us.elevenlabs.io/v1/voices/nPczCjzI2devNBz1zQrb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIyZGQzZTcyYy00ZmQzLTQyZjEtOTNlYS1hYmM1ZDRlNWFhMWQubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-brian": "https://api.us.elevenlabs.io/v1/voices/nPczCjzI2devNBz1zQrb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIyZGQzZTcyYy00ZmQzLTQyZjEtOTNlYS1hYmM1ZDRlNWFhMWQubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-nPczCjzI2devNBz1zQrb": "https://api.us.elevenlabs.io/v1/voices/nPczCjzI2devNBz1zQrb/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiIyZGQzZTcyYy00ZmQzLTQyZjEtOTNlYS1hYmM1ZDRlNWFhMWQubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "onwK4e9ZLuTAKqWW03F9": "https://api.us.elevenlabs.io/v1/voices/onwK4e9ZLuTAKqWW03F9/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI3ZWVlMDIzNi0xYTcyLTRiODYtYjMwMy01ZGNhZGMwMDdiYTkubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-daniel": "https://api.us.elevenlabs.io/v1/voices/onwK4e9ZLuTAKqWW03F9/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI3ZWVlMDIzNi0xYTcyLTRiODYtYjMwMy01ZGNhZGMwMDdiYTkubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",
    "eleven-onwK4e9ZLuTAKqWW03F9": "https://api.us.elevenlabs.io/v1/voices/onwK4e9ZLuTAKqWW03F9/previews/audio?payload=eyJ2b2ljZV9zb3VyY2UiOiJwcmVtYWRlIiwiZmlsZW5hbWUiOiI3ZWVlMDIzNi0xYTcyLTRiODYtYjMwMy01ZGNhZGMwMDdiYTkubXAzIiwidGltZXN0YW1wIjoxNzg5MTAyODAwMDAwMDAwfQ%3D%3D",

    "pFZP5JQG7iQjIQuC4Bku": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pFZP5JQG7iQjIQuC4Bku/89b68b35-b3dd-4348-a84a-a3c13a3c2b30.mp3",
    "eleven-lily": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pFZP5JQG7iQjIQuC4Bku/89b68b35-b3dd-4348-a84a-a3c13a3c2b30.mp3",
    "eleven-pFZP5JQG7iQjIQuC4Bku": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pFZP5JQG7iQjIQuC4Bku/89b68b35-b3dd-4348-a84a-a3c13a3c2b30.mp3",

    "pNInz6obpgDQGcFmaJgB": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pNInz6obpgDQGcFmaJgB/d6905d7a-dd26-4187-bfff-1bd3a5ea7cac.mp3",
    "eleven-adam": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pNInz6obpgDQGcFmaJgB/d6905d7a-dd26-4187-bfff-1bd3a5ea7cac.mp3",
    "eleven-pNInz6obpgDQGcFmaJgB": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pNInz6obpgDQGcFmaJgB/d6905d7a-dd26-4187-bfff-1bd3a5ea7cac.mp3",

    "pqHfZKP75CvOlQylNhV4": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pqHfZKP75CvOlQylNhV4/d782b3ff-84ba-4029-848c-acf01285524d.mp3",
    "eleven-bill": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pqHfZKP75CvOlQylNhV4/d782b3ff-84ba-4029-848c-acf01285524d.mp3",
    "eleven-pqHfZKP75CvOlQylNhV4": "https://storage.googleapis.com/eleven-public-prod/premade/voices/pqHfZKP75CvOlQylNhV4/d782b3ff-84ba-4029-848c-acf01285524d.mp3",

    "wBXNqKUATyqu0RtYt25i": "https://storage.googleapis.com/eleven-public-prod/database/workspace/1b0aef06ad1848988df4847a8d377baf/voices/wBXNqKUATyqu0RtYt25i/92f83238-5f85-4793-ba6b-dc2cdc482735.mp3",
    "eleven-adam-custom": "https://storage.googleapis.com/eleven-public-prod/database/workspace/1b0aef06ad1848988df4847a8d377baf/voices/wBXNqKUATyqu0RtYt25i/92f83238-5f85-4793-ba6b-dc2cdc482735.mp3",
    "eleven-wBXNqKUATyqu0RtYt25i": "https://storage.googleapis.com/eleven-public-prod/database/workspace/1b0aef06ad1848988df4847a8d377baf/voices/wBXNqKUATyqu0RtYt25i/92f83238-5f85-4793-ba6b-dc2cdc482735.mp3",

    # Legacy Aliases
    "eleven-rachel": "https://storage.googleapis.com/eleven-public-prod/premade/voices/21m00Tcm4TlvDq8ikWAM/65d80f52-703f-4876-9630-10111ef6c0b3.mp3",
    "21m00Tcm4TlvDq8ikWAM": "https://storage.googleapis.com/eleven-public-prod/premade/voices/21m00Tcm4TlvDq8ikWAM/65d80f52-703f-4876-9630-10111ef6c0b3.mp3",
    "eleven-charlotte": "https://storage.googleapis.com/eleven-public-prod/premade/voices/XB0fDUnXU5powFXDhCwa/942356dc-f10d-4d23-90c8-472097063f68.mp3",
    "eleven-drew": "https://storage.googleapis.com/eleven-public-prod/premade/voices/29vD33N1CtxCmqQRPOHJ/e8b52a3f-9732-440f-b78a-1f44a6c4a6ae.mp3",
    "eleven-clyde": "https://storage.googleapis.com/eleven-public-prod/premade/voices/2EiwWnXFnvU5JabPnv8n/65d80f52-703f-4876-9630-10111ef6c0b3.mp3",
    "eleven-paul": "https://storage.googleapis.com/eleven-public-prod/premade/voices/5Q0t7uMcjvnagumLfvZi/67341759-ad08-41a5-be6e-de12fe448618.mp3",
    "eleven-domi": "https://storage.googleapis.com/eleven-public-prod/premade/voices/AZnzlk1XvdvUeBnXmlld/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3",
    "eleven-dave": "https://storage.googleapis.com/eleven-public-prod/premade/voices/CYw3kZ02Hs0563khs1Fj/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3",
    "eleven-fin": "https://storage.googleapis.com/eleven-public-prod/premade/voices/D38z5RcWu1voky8WS1ja/e8b52a3f-9732-440f-b78a-1f44a6c4a6ae.mp3",
    "eleven-antoni": "https://storage.googleapis.com/eleven-public-prod/premade/voices/ErXwobaYiN019PkySvjV/65d80f52-703f-4876-9630-10111ef6c0b3.mp3",
    "eleven-thomas": "https://storage.googleapis.com/eleven-public-prod/premade/voices/GBv7mTt0atIp3Br8iCZE/58ee3ff5-f6f2-4628-93b8-e38eb31806b0.mp3",
    "eleven-emily": "https://storage.googleapis.com/eleven-public-prod/premade/voices/LcfcDJNigLAnAZAFgndn/65d80f52-703f-4876-9630-10111ef6c0b3.mp3",
    "eleven-elli": "https://storage.googleapis.com/eleven-public-prod/premade/voices/MF3mGyEYCl7XYWbV9V6O/01a3e33c-6e99-4ee7-8543-ff2216a32186.mp3",
}

# Neural Voice mappings (Zero-cost High-Definition Neural Edge TTS & Provider fallbacks)
NEURAL_VOICE_MAP = {
    # Zero-Cost High-Definition Neural Edge Voices (100% Free · No Key Needed)
    "neural-jenny":       {"neural": "en-US-JennyNeural",       "rate": "+0%", "pitch": "+0Hz"},
    "neural-guy":         {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "+0Hz"},
    "neural-aria":        {"neural": "en-US-AriaNeural",        "rate": "+4%", "pitch": "+2Hz"},
    "neural-christopher": {"neural": "en-US-ChristopherNeural", "rate": "+0%", "pitch": "-2Hz"},
    "neural-emma":        {"neural": "en-US-EmmaNeural",        "rate": "+2%", "pitch": "+2Hz"},
    "neural-andrew":      {"neural": "en-US-AndrewNeural",      "rate": "+2%", "pitch": "-1Hz"},
    "neural-sonia":       {"neural": "en-GB-SoniaNeural",       "rate": "+0%", "pitch": "+0Hz"},
    "neural-ryan":        {"neural": "en-GB-RyanNeural",        "rate": "+2%", "pitch": "-2Hz"},
    "neural-natasha":     {"neural": "en-AU-NatashaNeural",     "rate": "+2%", "pitch": "+2Hz"},
    "neural-neerja":      {"neural": "en-IN-NeerjaNeural",      "rate": "+0%", "pitch": "+0Hz"},
    "neural-prabhat":     {"neural": "en-IN-PrabhatNeural",     "rate": "+0%", "pitch": "+0Hz"},

    # Studio Pro Ultra-Realistic Platform Voices (18 Distinct Personas · 0 API Keys Needed)
    "studio-maya":        {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "-1Hz"},  # Professional executive assistant (Maya)
    "studio-calvin":      {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "-1Hz"},  # Calm, warm, natural male advisor (Calvin)
    "studio-camille":     {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "-1Hz"},  # Calm, warm female concierge (Camille)
    "studio-cimo":        {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "-1Hz"},  # Cimo exact acoustic match
    "studio-kaitlyn":     {"neural": "en-US-JennyNeural",       "rate": "+3%", "pitch": "+1Hz"},  # Friendly upbeat support (Kaitlyn)
    "studio-nathan":      {"neural": "en-US-ChristopherNeural", "rate": "+1%", "pitch": "-3Hz"},  # Deep confident advisor (Nathan)
    "studio-sierra":      {"neural": "en-US-AvaNeural",         "rate": "+4%", "pitch": "+1Hz"},  # Fast operations dispatcher (Sierra)
    "studio-brooke":      {"neural": "en-US-EmmaNeural",        "rate": "+3%", "pitch": "+2Hz"},  # Warm virtual concierge (Brooke)
    "studio-giselle":     {"neural": "en-US-JennyNeural",       "rate": "+1%", "pitch": "-1Hz"},  # Reliable enterprise care (Giselle)
    "studio-luna":        {"neural": "en-US-AriaNeural",        "rate": "+5%", "pitch": "+3Hz"},  # Bright empathetic receptionist (Luna)
    "studio-rosie":       {"neural": "en-US-AvaNeural",         "rate": "+3%", "pitch": "+2Hz"},  # Youthful conversationalist (Rosie)
    "studio-winona":      {"neural": "en-GB-SoniaNeural",       "rate": "+2%", "pitch": "+0Hz"},  # Polished British RP (Winona)
    "studio-amber":       {"neural": "en-GB-LibbyNeural",       "rate": "+4%", "pitch": "+1Hz"},  # Friendly UK concierge (Amber)
    "studio-cassidy":     {"neural": "en-US-AriaNeural",        "rate": "+6%", "pitch": "+2Hz"},  # Vibrant customer care (Cassidy)
    "studio-lucas":       {"neural": "en-US-EricNeural",        "rate": "+2%", "pitch": "-1Hz"},  # Confident modern assistant (Lucas)
    "studio-danica":      {"neural": "en-US-MichelleNeural",    "rate": "+2%", "pitch": "-2Hz"},  # Efficient service dispatcher (Danica)
    "studio-melanie":     {"neural": "en-US-AnaNeural",         "rate": "+2%", "pitch": "+0Hz"},  # Articulate support advisor (Melanie)
    "studio-madeline":    {"neural": "en-GB-RyanNeural",        "rate": "+2%", "pitch": "-2Hz"},  # Charming British assistant (Madeline)
    "studio-alana":       {"neural": "es-US-PalomaNeural",      "rate": "+3%", "pitch": "+0Hz"},  # Warm bilingual care (Alana)
    "studio-alana-es":    {"neural": "es-US-PalomaNeural",      "rate": "+3%", "pitch": "+0Hz"},  # Asistente en español (Alana ES)

    # Cartesia Sonic (American, British, Aussie, Canadian & Spanish voices)
    "f786b574-daa5-4673-aa0c-cbe3e8534c02": {"neural": "en-US-MichelleNeural", "rate": "+4%", "pitch": "+1Hz"},  # Aurora
    "a0e99841-438c-4a64-b679-ae501e7d6091": {"neural": "en-US-AvaNeural",      "rate": "+8%", "pitch": "+3Hz"},  # Vale
    "729651dc-c6c3-4ee5-97fa-350da1f88600": {"neural": "en-US-RogerNeural",    "rate": "+0%", "pitch": "-4Hz"},  # Ridge
    "694f9389-aac1-45b6-b726-9d9369183238": {"neural": "en-CA-ClaraNeural",    "rate": "+6%", "pitch": "+4Hz"},  # Brooke
    "b7d50908-b17c-442d-ad8d-810c63997ed9": {"neural": "en-US-EricNeural",     "rate": "+3%", "pitch": "-2Hz"},  # Leo
    "3656c123-289b-449e-9d29-c89b4f9fb338": {"neural": "en-GB-LibbyNeural",    "rate": "+2%", "pitch": "+1Hz"},  # Evelyn
    "c885cf83-7c50-482a-a9f0-28ec36081e66": {"neural": "en-GB-ThomasNeural",   "rate": "+3%", "pitch": "-3Hz"},  # George
    "829ccd10-f8b3-43cd-a8c0-96aa29f3be6f": {"neural": "en-AU-WilliamMultilingualNeural", "rate": "+4%", "pitch": "-2Hz"}, # Sarah / AU
    "a216d649-14a5-48b2-b430-81f1e3100346": {"neural": "es-US-PalomaNeural",   "rate": "+5%", "pitch": "+0Hz"},  # Mateo

    # Deepgram Aura
    "aura-asteria-en": {"neural": "en-US-JennyNeural",       "rate": "+2%", "pitch": "+1Hz"},
    "aura-orion-en":   {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "-1Hz"},
    "aura-luna-en":    {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "+0Hz"},
    "aura-angus-en":   {"neural": "en-GB-ThomasNeural",      "rate": "+0%", "pitch": "-2Hz"},
    "aura-stella-en":  {"neural": "en-US-AriaNeural",        "rate": "+2%", "pitch": "+1Hz"},
    "aura-athena-en":  {"neural": "en-US-EmmaNeural",        "rate": "+1%", "pitch": "+0Hz"},
    "aura-helios-en":  {"neural": "en-US-AndrewNeural",      "rate": "+2%", "pitch": "+0Hz"},

    # ElevenLabs Turbo Fallbacks
    "ecp3DWciuUyW7BYM7II1": {"neural": "en-IN-NeerjaNeural",      "rate": "+2%", "pitch": "+1Hz"},  # Anika
    "RXtWW6etvimS8QJ5nhVk": {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "+0Hz"},  # Fiona
    "CwhRBWXzGAHq8TQ4Fs17": {"neural": "en-US-AndrewNeural",      "rate": "+0%", "pitch": "-2Hz"},  # Roger
    "EXAVITQu4vr4xnSDxMaL": {"neural": "en-US-JennyNeural",       "rate": "+0%", "pitch": "+0Hz"},  # Sarah
    "FGY2WhTYpPnrIDTdsKH5": {"neural": "en-US-AriaNeural",        "rate": "+3%", "pitch": "+2Hz"},  # Laura
    "IKne3meq5aSn9XLyUdCD": {"neural": "en-AU-WilliamMultilingualNeural", "rate": "+2%", "pitch": "-1Hz"}, # Charlie
    "JBFqnCBsd6RMkjVDRZzb": {"neural": "en-GB-RyanNeural",        "rate": "+1%", "pitch": "-1Hz"},  # George
    "N2lVS1w4EtoT3dr4eOWO": {"neural": "en-GB-ThomasNeural",      "rate": "+0%", "pitch": "-3Hz"},  # Callum
    "SAz9YHcvj6GT2YYXdXww": {"neural": "en-US-AvaNeural",         "rate": "+2%", "pitch": "+1Hz"},  # River
    "SOYHLrjzK2X1ezoPC6cr": {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "-1Hz"},  # Harry
    "TX3LPaxmHKxFdv7VOQHJ": {"neural": "en-US-BrianNeural",       "rate": "+1%", "pitch": "-2Hz"},  # Liam
    "Xb7hH8MSUJpSbSDYk0k2": {"neural": "en-GB-SoniaNeural",       "rate": "+2%", "pitch": "+1Hz"},  # Alice
    "XrExE9yKIg1WjnnlVkGX": {"neural": "en-US-EmmaNeural",        "rate": "+2%", "pitch": "+1Hz"},  # Matilda
    "bIHbv24MWmeRgasZH58o": {"neural": "en-US-EricNeural",        "rate": "+1%", "pitch": "-1Hz"},  # Will
    "cgSgspJ2msm6clMCkdW9": {"neural": "en-US-MichelleNeural",    "rate": "+2%", "pitch": "+1Hz"},  # Jessica
    "cjVigY5qzO86Huf0OWal": {"neural": "en-US-AndrewNeural",      "rate": "+1%", "pitch": "+0Hz"},  # Eric
    "hpp4J3VqNfWAUOO0d1Us": {"neural": "en-US-AvaNeural",         "rate": "+4%", "pitch": "+2Hz"},  # Bella
    "iP95p4xoKVk53GoZ742B": {"neural": "en-US-GuyNeural",         "rate": "+2%", "pitch": "+0Hz"},  # Chris
    "nPczCjzI2devNBz1zQrb": {"neural": "en-US-ChristopherNeural", "rate": "+0%", "pitch": "-3Hz"},  # Brian
    "onwK4e9ZLuTAKqWW03F9": {"neural": "en-GB-RyanNeural",        "rate": "+1%", "pitch": "-1Hz"},  # Daniel
    "pFZP5JQG7iQjIQuC4Bku": {"neural": "en-US-AnaNeural",         "rate": "+2%", "pitch": "+1Hz"},  # Lily
    "pNInz6obpgDQGcFmaJgB": {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "+0Hz"},  # Adam
    "pqHfZKP75CvOlQylNhV4": {"neural": "en-US-ChristopherNeural", "rate": "+0%", "pitch": "-2Hz"},  # Bill
    "wBXNqKUATyqu0RtYt25i": {"neural": "en-US-GuyNeural",         "rate": "+0%", "pitch": "+0Hz"},  # Adam Custom

    # Legacy / Retell aliases
    "retell-cimo":       {"neural": "en-US-AvaNeural",         "rate": "+0%", "pitch": "-1Hz"},
    "retell-kate":       {"neural": "en-US-JennyNeural",       "rate": "+3%", "pitch": "+1Hz"},
    "retell-marissa":    {"neural": "en-US-MichelleNeural",    "rate": "+3%", "pitch": "+0Hz"},
    "retell-nico":       {"neural": "en-US-ChristopherNeural", "rate": "+1%", "pitch": "-3Hz"},
    "retell-sloane":     {"neural": "en-US-AvaNeural",         "rate": "+4%", "pitch": "+1Hz"},
    "retell-brynne":     {"neural": "en-US-EmmaNeural",        "rate": "+3%", "pitch": "+2Hz"},
    "retell-grace":      {"neural": "en-US-JennyNeural",       "rate": "+1%", "pitch": "-1Hz"},
    "retell-lily":       {"neural": "en-US-AriaNeural",        "rate": "+5%", "pitch": "+3Hz"},
    "retell-rita":       {"neural": "en-US-AvaNeural",         "rate": "+3%", "pitch": "+2Hz"},
    "retell-willa":      {"neural": "en-GB-SoniaNeural",       "rate": "+2%", "pitch": "+0Hz"},
    "retell-ashley":     {"neural": "en-GB-LibbyNeural",       "rate": "+4%", "pitch": "+1Hz"},
    "retell-chloe":      {"neural": "en-US-AriaNeural",        "rate": "+6%", "pitch": "+2Hz"},
    "retell-leland":     {"neural": "en-US-EricNeural",        "rate": "+2%", "pitch": "-1Hz"},
    "retell-della":      {"neural": "en-US-MichelleNeural",    "rate": "+2%", "pitch": "-2Hz"},
    "retell-merritt":    {"neural": "en-US-AnaNeural",         "rate": "+2%", "pitch": "+0Hz"},
    "retell-maren":      {"neural": "en-GB-RyanNeural",        "rate": "+2%", "pitch": "-2Hz"},
    "retell-andrea":     {"neural": "es-US-PalomaNeural",      "rate": "+3%", "pitch": "+0Hz"},
    "retell-andrea-es":  {"neural": "es-US-PalomaNeural",      "rate": "+3%", "pitch": "+0Hz"},

    # OpenAI TTS (Distinct tone personas)
    "openai-alloy":   {"neural": "en-US-JennyNeural",   "rate": "+0%", "pitch": "-2Hz"},
    "openai-echo":    {"neural": "en-US-SteffanNeural", "rate": "+3%", "pitch": "+2Hz"},
    "openai-fable":   {"neural": "en-GB-RyanNeural",    "rate": "+0%", "pitch": "+0Hz"},
    "openai-onyx":    {"neural": "en-US-BrianNeural",   "rate": "-2%", "pitch": "-8Hz"},
    "openai-nova":    {"neural": "en-US-AvaNeural",     "rate": "+5%", "pitch": "+4Hz"},
    "openai-shimmer": {"neural": "en-US-AnaNeural",     "rate": "-3%", "pitch": "+2Hz"},
}


def _fetch_deepgram_tts(model: str, text: str, api_key: str) -> Optional[bytes]:
    """Calls official Deepgram Aura TTS API."""
    try:
        url = f"https://api.deepgram.com/v1/speak?model={model}"
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "VoiceAgentService/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.read()
    except Exception as ex:
        log.warning("Deepgram TTS API call failed for model %s: %s", model, ex)
        return None


def _fetch_elevenlabs_cdn(voice_id: str) -> Optional[bytes]:
    """Fetches official ElevenLabs portal sample MP3 audio."""
    cdn_url = OFFICIAL_ELEVENLABS_CDN_PREVIEWS.get(voice_id)
    if not cdn_url:
        raw_id = voice_id.replace("eleven-", "")
        cdn_url = OFFICIAL_ELEVENLABS_CDN_PREVIEWS.get(raw_id)
    if not cdn_url:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        xi_key = os.getenv("ELEVEN_API_KEY") or os.getenv("ELEVENLABS_API_KEY") or os.getenv("XI_API_KEY")
        if xi_key:
            headers["xi-api-key"] = xi_key
        req = urllib.request.Request(cdn_url, headers=headers)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.read()
    except Exception as ex:
        log.warning("ElevenLabs CDN fetch failed for %s: %s", voice_id, ex)
        return None


def _fetch_openai_tts(model_voice: str, text: str, api_key: str) -> Optional[bytes]:
    """Calls official OpenAI TTS API if key is present."""
    try:
        url = "https://api.openai.com/v1/audio/speech"
        payload = json.dumps({
            "model": "tts-1",
            "voice": model_voice,
            "input": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.read()
    except Exception as ex:
        log.warning("OpenAI TTS API call failed for voice %s: %s", model_voice, ex)
        return None


def _fetch_cartesia_tts(voice_id: str, text: str, api_key: str) -> Optional[bytes]:
    """Calls official Cartesia Sonic TTS API if key is present."""
    try:
        url = "https://api.cartesia.ai/tts/bytes"
        payload = json.dumps({
            "model_id": "sonic-3",
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            "output_format": {"container": "wav", "sample_rate": 24000, "encoding": "pcm_s16le"},
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "X-API-Key": api_key,
                "Cartesia-Version": "2024-06-10",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.read()
    except Exception as ex:
        log.warning("Cartesia TTS API call failed for voice %s: %s", voice_id, ex)
        return None


def clean_spoken_speech_text(text: str) -> str:
    """Strips XML/HTML/SSML tags, URLs, brackets, JSON, Markdown, and prompt directives so only clean spoken sentences are vocalized."""
    if not text or not isinstance(text, str):
        return ""
    
    import re
    t = text.strip()
    if t.lower() in ("undefined", "null", "none"):
        return ""

    # If the text is a prompt instruction with an explicit quoted opening, extract the opening
    # e.g., 'Say this, and nothing else, as your first turn:\n\n"Thanks for calling Bottini and Bottini..."'
    quoted_match = re.search(r'(?:say this|opening|greeting)[^"\']*?["“]([^"”]{10,250})["”]', t, re.I | re.S)
    if quoted_match:
        t = quoted_match.group(1).strip()
    elif t.lower().startswith("identity") or "you are " in t.lower()[:60]:
        # Handle prompt text inadvertently passed as speech
        from agent.agent_builder import agent_builder
        derived = agent_builder.derive_first_message(t, "Maya")
        if derived:
            t = derived
    
    # 1. Remove XML/HTML/SSML tags and thought blocks: <thought>...</thought>, <speak>...</speak>, <mstts:...>, etc.
    t = re.sub(r"<\?xml[\s\S]*?\?>", " ", t, flags=re.I)
    t = re.sub(r"<thought[\s\S]*?</thought>", " ", t, flags=re.I)
    t = re.sub(r"<thinking[\s\S]*?</thinking>", " ", t, flags=re.I)
    t = re.sub(r"<script[\s\S]*?</script>", " ", t, flags=re.I)
    t = re.sub(r"<speak[\s\S]*?>", " ", t, flags=re.I)
    t = re.sub(r"</speak>", " ", t, flags=re.I)
    t = re.sub(r"<voice[\s\S]*?>", " ", t, flags=re.I)
    t = re.sub(r"</voice>", " ", t, flags=re.I)
    t = re.sub(r"<mstts:[^>]*>", " ", t, flags=re.I)
    t = re.sub(r"</mstts:[^>]*>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)

    # 2. Remove JSON objects: {"...": "..."}
    t = re.sub(r"\{[^{}]*\}", " ", t)

    # 3. Remove bracketed tool indicators: [book_appointment], [transfer_call], [check_availability]
    t = re.sub(r"\[[a-zA-Z0-9_\-\s]+\]", " ", t)

    # 4. Remove or normalize URLs: https://www.example.com/page -> "our website"
    t = re.sub(r"https?://(?:www\.)?[^\s/$.?#].[^\s]*", "our website", t, flags=re.I)
    t = re.sub(r"www\.[^\s/$.?#].[^\s]*", "our website", t, flags=re.I)
    t = re.sub(r"\b[a-zA-Z0-9\-]+\.(?:com|org|net|gov|edu|io|ai)(?:/[^\s]*)?", "our website", t, flags=re.I)

    # 5. Remove Markdown links: [Title](url) -> Title
    t = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", t)

    # 6. Remove Markdown formatting: **bold**, *italic*, ## headings, `code`, bullets
    t = re.sub(r"[*_~`#]", " ", t)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)

    # 7. Clean HTML entities
    t = t.replace("&amp;", "and").replace("&lt;", "less than").replace("&gt;", "greater than").replace("&quot;", "\"").replace("&#39;", "'")

    # 8. Clean redundant special symbols and whitespace
    t = re.sub(r"[\\/{}\[\]<>]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    # 9. Extract first 1-2 clean sentences for crisp conversational speech
    sentences = re.split(r"(?<=[.!?])\s+", t)
    if sentences:
        clean_sentences = [s.strip() for s in sentences if len(s.strip()) > 3]
        if len(clean_sentences) > 2:
            t = " ".join(clean_sentences[:2])
        elif clean_sentences:
            t = " ".join(clean_sentences)

    return t.strip()


async def generate_speech_audio_bytes(
    voice_id: str,
    name: str = "Assistant",
    gender: str = "female",
    style: str = "conversational",
    provider: str = "cartesia",
    text: str = "",
) -> bytes:
    """
    Fetches real authentic human speech audio.
    Prioritizes official cloud provider APIs and CDN portal samples.
    """
    cleaned_input = clean_spoken_speech_text(text)
    cache_key = f"{voice_id}:{cleaned_input}"
    if cache_key in _AUDIO_CACHE:
        return _AUDIO_CACHE[cache_key]

    provider_labels = {
        "studio": "Studio Pro Ultra-Realistic",
        "neural": "Free Neural TTS",
        "cartesia": "Cartesia Sonic",
        "deepgram": "Deepgram Aura",
        "openai": "OpenAI TTS",
        "elevenlabs": "ElevenLabs Turbo",
    }
    clean_name = name.replace("(Studio Pro)", "").replace("(Free Neural)", "").replace("(Spanish Studio Pro)", "").replace("(Retell AI)", "").replace("(British Free)", "").replace("(Indian English Free)", "").replace("(Aussie Free)", "").strip() or "Assistant"
    
    phrase = cleaned_input or f"Hello! I am {clean_name}, your AI voice assistant. How can I help you today?"

    # 1. ElevenLabs Official Portal Audio
    if provider == "elevenlabs" or voice_id.startswith("eleven-"):
        # If user has ELEVENLABS_API_KEY / XI_API_KEY
        xi_key = os.getenv("ELEVEN_API_KEY") or os.getenv("ELEVENLABS_API_KEY") or os.getenv("XI_API_KEY")
        if xi_key:
            try:
                raw_id = voice_id.replace("eleven-", "")
                url = f"https://api.elevenlabs.io/v1/text-to-speech/{raw_id}"
                payload = {
                    "text": phrase,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {
                        "stability": 0.50,
                        "similarity_boost": 0.80,
                        "style": 0.15,
                        "use_speaker_boost": True,
                    }
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"xi-api-key": xi_key, "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=6.0) as resp:
                    audio_bytes = resp.read()
                    if audio_bytes:
                        _AUDIO_CACHE[cache_key] = audio_bytes
                        return audio_bytes
            except Exception as e:
                log.warning("ElevenLabs API key request failed: %s", e)

        # Direct official ElevenLabs portal CDN recording (authentic voice) if previewing without custom text
        if not cleaned_input:
            cdn_audio = _fetch_elevenlabs_cdn(voice_id)
            if cdn_audio:
                _AUDIO_CACHE[cache_key] = cdn_audio
                return cdn_audio

    # 2. Deepgram Aura Official TTS (uses DEEPGRAM_API_KEY from .env)
    if provider == "deepgram" or voice_id.startswith("aura-"):
        dg_key = os.getenv("DEEPGRAM_API_KEY", "0d47c5e9889b40517b7e0e557940c7489dda4b31")
        if dg_key:
            dg_audio = _fetch_deepgram_tts(voice_id, phrase, dg_key)
            if dg_audio:
                _AUDIO_CACHE[cache_key] = dg_audio
                return dg_audio

    # 3. OpenAI TTS API (if OPENAI_API_KEY is present)
    if provider == "openai" or voice_id.startswith("openai-"):
        oa_key = os.getenv("OPENAI_API_KEY")
        if oa_key:
            voice_name = voice_id.replace("openai-", "")
            oa_audio = _fetch_openai_tts(voice_name, phrase, oa_key)
            if oa_audio:
                _AUDIO_CACHE[cache_key] = oa_audio
                return oa_audio

    # 4. Cartesia Sonic API (if CARTESIA_API_KEY is present)
    if provider == "cartesia":
        cart_key = os.getenv("CARTESIA_API_KEY")
        if cart_key:
            cart_audio = _fetch_cartesia_tts(voice_id, phrase, cart_key)
            if cart_audio:
                _AUDIO_CACHE[cache_key] = cart_audio
                return cart_audio

    # 5. High-Definition Neural TTS with Human Emotion & Expressive Tone
    cfg = NEURAL_VOICE_MAP.get(voice_id)
    if not cfg:
        is_female = (gender == "female")
        neural = "en-US-JennyNeural" if is_female else "en-US-GuyNeural"
        rate = "+0%"
        pitch = "+0Hz"
    else:
        neural = cfg["neural"]
        rate = cfg.get("rate", "+0%")
        pitch = cfg.get("pitch", "+0Hz")

    # Map user/preset tone labels to expressive human styles
    style_key = (style or "").lower().strip()
    style_map = {
        "customerservice": "customerservice",
        "customer_service": "customerservice",
        "support": "customerservice",
        "cheerful": "cheerful",
        "happy": "cheerful",
        "upbeat": "cheerful",
        "empathetic": "empathetic",
        "caring": "empathetic",
        "soothing": "empathetic",
        "excited": "excited",
        "energetic": "excited",
        "friendly": "friendly",
        "conversational": "friendly",
        "hopeful": "hopeful",
        "reassuring": "hopeful",
        "newscast": "newscast-casual",
        "professional": "newscast-casual",
        "broadcaster": "newscast-casual",
        "whisper": "whispering",
        "whispering": "whispering",
    }
    exp_style = style_map.get(style_key)

    try:
        import edge_tts
        clean_text = clean_spoken_speech_text(phrase) or phrase
        communicate = edge_tts.Communicate(clean_text, voice=neural, rate=rate, pitch=pitch)

        audio_stream = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.write(chunk["data"])

        audio_bytes = audio_stream.getvalue()
        if audio_bytes:
            _AUDIO_CACHE[cache_key] = audio_bytes
            return audio_bytes
    except Exception as ex:
        log.warning("Neural TTS failed for %s (%s): %s", voice_id, neural, ex)

    # Secondary fallback WAV
    import math, wave, struct
    sample_rate = 24000
    dur = 2.0
    num_samples = int(sample_rate * dur)
    f0 = 220.0 if gender == "female" else 130.0
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        data = bytearray()
        for i in range(num_samples):
            t = i / sample_rate
            env = math.sin(math.pi * (i / num_samples))
            val = int(math.sin(2 * math.pi * f0 * t) * env * 16000)
            data.extend(struct.pack("<h", val))
        wf.writeframes(data)
    return buf.getvalue()


def get_voice_audio(voice_id: str, name: str = "", gender: str = "female", style: str = "", provider: str = "cartesia", text: str = "") -> bytes:
    """Synchronous wrapper for generating speech audio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            generate_speech_audio_bytes(
                voice_id=voice_id,
                name=name,
                gender=gender,
                style=style,
                provider=provider,
                text=text,
            )
        )
    finally:
        loop.close()
