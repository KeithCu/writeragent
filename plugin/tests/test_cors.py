import re

def is_safe_origin(origin):
    # Safe domains: localhost, 127.0.0.1, [::1]
    # Optionally followed by :PORT
    pattern = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
    return bool(pattern.match(origin))

origins = [
    "http://localhost",
    "https://localhost",
    "http://localhost:3000",
    "https://localhost:8443",
    "http://127.0.0.1",
    "https://127.0.0.1",
    "http://127.0.0.1:8080",
    "http://[::1]",
    "http://[::1]:3000",
    "http://localhost.attacker.com",
    "https://127.0.0.1.badguy.com",
    "http://example.com"
]

for o in origins:
    print(f"{o}: {is_safe_origin(o)}")
