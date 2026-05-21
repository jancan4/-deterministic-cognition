import json

from . import service


def export_to_file(db_path: str, out_path: str) -> dict:
    payload = service.export_memory(db_path)
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write('\n')
    return payload
