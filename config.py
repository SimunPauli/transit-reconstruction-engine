from config_loader import load_config

_config = None

def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config