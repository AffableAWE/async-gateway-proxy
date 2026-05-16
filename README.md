# PyGate

### Request Flow
The current execution logic follows these steps:

1. **Receive Request**
2. **Find Matching Upstream**
3. **Call `forward_request`**
4. **Return Response**
---

### Quick Logic Reference

```text
Client Request 
      ↓
Identify Upstream
      ↓
forward_request()
      ↓
Client Response
