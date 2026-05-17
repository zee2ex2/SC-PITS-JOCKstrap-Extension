import json
import importlib
import pkgutil
from pathlib import Path


class Extension:
    name = ""
    version = "1.0"
    description = ""
    settings_template = None

    def on_startup(self, globals_dict):
        pass

    def on_route(self, path, qs, data, method):
        return None, False

    def on_inventory_add(self, db, inv_id, data):
        pass

    def on_inventory_update(self, db, inv_id, data):
        pass

    def on_inventory_delete(self, db, inv_id):
        pass

    def get_context(self):
        return {}

    def get_settings_html(self):
        if self.settings_template:
            return self.settings_template
        return ""


def discover_extensions():
    extensions = []
    ext_dir = Path(__file__).resolve().parent
    for entry in ext_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("_") or entry.name == "__pycache__":
            continue
        init_file = entry / "__init__.py"
        if not init_file.exists():
            continue
        try:
            mod = importlib.import_module(f"extensions.{entry.name}")
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and issubclass(attr, Extension) and attr is not Extension:
                    ext = attr()
                    extensions.append(ext)
        except Exception as e:
            print(f"Extension load failed: {entry.name} - {e}")
    return extensions
