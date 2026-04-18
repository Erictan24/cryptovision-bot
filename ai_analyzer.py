"""
ai_analyzer.py — AI scoring untuk filter signal sebelum eksekusi.

Pakai Claude API untuk analisa konteks market dan beri confidence score.
Dipanggil setelah semua filter lain lolos — sebagai final gate sebelum auto trade.

Cara kerja:
  1. Bot generate signal normal (SMC + S&R + candle + volume)
  2. Kalau signal GOOD atau LIMIT at_zone → kirim ke AI untuk review
  3. AI beri score 1-10 dan reasoning
  4. Score < 6 → skip trade, score >= 6 → lanjut eksekusi

Hemat API call — hanya dipanggil kalau signal sudah melewati semua filter.
"""

import json
import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
AI_ENABLED    = os.getenv('AI_SCORING_ENABLED', 'false').lower() == 'true'
AI_MIN_SCORE  = int(os.getenv('AI_MIN_SCORE', '6'))  # minimum score untuk eksekusi


def ai_score_signal(signal: dict, market_context: dict) -> dict:
    """
    Minta Claude untuk review signal dan beri confidence score.

    Returns:
        {
            'score': 7,           # 1-10
            'confidence': 'HIGH', # HIGH/MEDIUM/LOW
            'reasoning': '...',
            'approved': True,
            'ai_used': True,
        }
    """
    if not AI_ENABLED:
        return {'score': 7, 'approved': True, 'ai_used': False, 'reasoning': 'AI scoring nonaktif'}

    try:
        direction = signal.get('direction', '')
        quality   = signal.get('quality', '')
        symbol    = signal.get('symbol', signal.get('coin', ''))
        entry     = signal.get('entry', 0)
        sl        = signal.get('sl', 0)
        tp1       = signal.get('tp1', 0)
        tp2       = signal.get('tp2', 0)
        score     = signal.get('confluence_score', 0)
        reasons   = signal.get('reasons', [])
        candle_ok = signal.get('candle_confirmed', None)
        vol_ratio = signal.get('volume_ratio', 1.0)
        timing_ok = signal.get('15m_confirmed', None)

        price     = market_context.get('price', 0)
        chg24     = market_context.get('change_24h', 0)
        structure = market_context.get('structure', '')
        bias      = market_context.get('market_bias', '')

        prompt = f"""Kamu adalah analis trading crypto profesional. Review signal berikut dan beri score kepercayaan.

SIGNAL:
- Coin: {symbol}
- Arah: {direction}
- Quality: {quality}
- Entry: {entry}
- SL: {sl} (risk: {abs(entry-sl)/entry*100:.2f}%)
- TP1: {tp1} | TP2: {tp2} (RR 1:2)
- Confluence Score: {score}
- Candle confirmation: {'Ada ✅' if candle_ok else 'Tidak ada ❌' if candle_ok is False else 'N/A'}
- 15m timing: {'Bagus ✅' if timing_ok else 'Belum ❌' if timing_ok is False else 'N/A'}
- Volume ratio: {vol_ratio}x rata-rata

ALASAN SIGNAL:
{chr(10).join(f'- {r}' for r in reasons[:5])}

KONTEKS MARKET:
- Harga saat ini: {price}
- Perubahan 24h: {chg24:+.2f}%
- Struktur: {structure}
- Bias: {bias}

Berikan response HANYA dalam format JSON berikut (tidak ada teks lain):
{{"score": <1-10>, "confidence": "<HIGH|MEDIUM|LOW>", "approved": <true|false>, "reasoning": "<1-2 kalimat singkat alasan>"}}

Score 1-4: Jangan eksekusi (setup lemah/berbahaya)
Score 5-6: Bisa eksekusi tapi hati-hati
Score 7-8: Setup bagus, eksekusi dengan normal risk
Score 9-10: Setup sangat kuat, konfirmasi semua layer"""

        response = requests.post(
            ANTHROPIC_API,
            headers={
                "Content-Type": "application/json",
                "x-api-key": os.getenv('ANTHROPIC_API_KEY', ''),
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",  # Hemat — pakai Haiku
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15,
        )

        if response.status_code != 200:
            logger.warning(f"AI API error {response.status_code} — skip AI scoring")
            return {'score': 7, 'approved': True, 'ai_used': False, 'reasoning': 'API error'}

        content = response.json().get('content', [{}])[0].get('text', '{}')
        result  = json.loads(content.strip())

        score_ai  = int(result.get('score', 5))
        approved  = result.get('approved', score_ai >= AI_MIN_SCORE)
        reasoning = result.get('reasoning', '')
        conf      = result.get('confidence', 'MEDIUM')

        logger.info(f"🤖 AI score {symbol} {direction}: {score_ai}/10 ({conf}) — {'✅ APPROVED' if approved else '❌ REJECTED'}")
        if reasoning:
            logger.info(f"   Reasoning: {reasoning}")

        return {
            'score'     : score_ai,
            'confidence': conf,
            'approved'  : approved,
            'ai_used'   : True,
            'reasoning' : reasoning,
        }

    except json.JSONDecodeError:
        logger.warning("AI response bukan JSON valid — skip AI scoring")
        return {'score': 7, 'approved': True, 'ai_used': False}
    except Exception as e:
        logger.warning(f"AI scoring error: {e} — lanjut tanpa AI")
        return {'score': 7, 'approved': True, 'ai_used': False}
