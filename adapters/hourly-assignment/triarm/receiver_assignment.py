"""Convert only the sealed assignment's endpoints into Kiwi recorder rows."""
from urllib.parse import urlsplit
from assignment import AssignmentError

def assigned_receivers(assignment):
    rows=[]
    for item in assignment["assigned_sdrs"]:
        endpoint=item["endpoint"].strip()
        parsed=urlsplit(endpoint if "://" in endpoint else "//"+endpoint)
        if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.hostname or parsed.port is None:
            raise AssignmentError(f"unsupported assigned SDR endpoint: {endpoint}")
        rows.append({"candidate_id":item["receiver_id"],"host":parsed.hostname,"port":parsed.port,"site":item["site"],"endpoint":endpoint})
    return rows
