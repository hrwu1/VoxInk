"""Lightweight public identity of the local helper, shared with its launcher.

Keep this outside ``services.transcription``: importing that package constructs
the service's API imports, which the process supervisor does not need.
"""

PROTOCOL_VERSION = 1
SERVICE_VERSION = "0.3.0"
