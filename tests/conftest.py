import os

# Importing config requires these; tests never touch the network.
os.environ.setdefault("AISSTREAM_API_KEY", "test")
os.environ.setdefault("MONGODB_URI", "mongodb://test")
