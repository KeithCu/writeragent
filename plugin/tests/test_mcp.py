import urllib.request
import urllib.error
import json
import time

def check_endpoint(name, url, method="GET", data=None):
    print(f"\n--- Testing {name} ---")
    print(f"URL: {url}")
    
    req = urllib.request.Request(url, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(data).encode('utf-8')
    
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, data=data, timeout=10) as response:
            status = response.getcode()
            body = response.read().decode('utf-8')
            elapsed = time.time() - start_time
            
            print(f"Status: {status} (Took {elapsed:.2f}s)")
            try:
                parsed = json.loads(body)
                print("Response JSON:")
                print(json.dumps(parsed, indent=2))
            except json.JSONDecodeError:
                print("Response Body:")
                print(body)
                
    except urllib.error.URLError as e:
        elapsed = time.time() - start_time
        print(f"Error: {e} (Took {elapsed:.2f}s)")
    except Exception as e:
        print(f"Unexpected error: {e}")

if __name__ == "__main__":
    BASE_URL = "http://127.0.0.1:8765"
    print("Testing WriterAgent MCP Server...")
    
    # 1. Test Health
    check_endpoint("Health", f"{BASE_URL}/health")
    
    # 2. Test Documents (Needs UNO main thread access)
    check_endpoint("Documents", f"{BASE_URL}/documents")
    
    # 3. Test Tools (Needs UNO main thread access)
    check_endpoint("Tools", f"{BASE_URL}/tools")
