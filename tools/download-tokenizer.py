"""Download pinned tokenizer JSON data; never execute archive contents."""
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

URL = 'https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'
HASHES = {
    'tokenizer.json': '89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614',
    'tokenizer_config.json': '841f8cf146e3f0ad1082594a31f68ecf7608c20467ef355081333d85bbaeb1cb',
}


def main():
    destination = Path(__file__).resolve().parents[1] / 'assets/deepseek-v4-tokenizer'
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    verified = {}
    with archive:
        for name, digest in HASHES.items():
            matches = [entry for entry in archive.infolist() if not entry.is_dir() and Path(entry.filename).name == name]
            if len(matches) != 1:
                raise ValueError(f'Expected exactly one {name}')
            data = archive.read(matches[0])
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f'Tokenizer checksum mismatch: {name}')
            verified[name] = data
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in verified.items():
        (destination / name).write_bytes(data)
    print('Verified and saved tokenizer JSON data; no model weights or code downloaded.')


if __name__ == '__main__':
    main()
