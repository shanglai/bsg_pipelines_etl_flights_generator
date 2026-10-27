"""Mide la frecuencia de 5xx de la API con N peticiones espaciadas bajo el límite (60/min)."""
import json
import sys
import time
from collections import Counter

import requests

n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
api = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000"
c = Counter()
for _ in range(n):
    r = requests.get(f"{api}/v1/eventos", headers={"X-API-Key": "llave-local-123"}, params={"limite": 1}, timeout=10)
    c[r.status_code] += 1
    time.sleep(1.05)
cinco = sum(v for k, v in c.items() if k >= 500)
print(json.dumps({"peticiones": n, "codigos": dict(c), "tasa_5xx": round(cinco / n, 4)}))
