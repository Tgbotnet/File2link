class InvalidHash(Exception):
    message = "Invalid hash"

class FIleNotFound(Exception):
    message = "File not found"

class LinkExpired(FIleNotFound):
    """Subclasses FIleNotFound so every existing handler keeps working unchanged."""
    message = "This link has expired"
