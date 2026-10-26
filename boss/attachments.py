"""Captain images for workers: checked here, uploaded to the worker's T3 host, attached by id."""
from pathlib import Path

from .common import BossError


MAX_IMAGES = 8  # t3_thread_send_attachments maxItems
MAX_BYTES = 10 * 1024 * 1024  # T3's image upload limit
SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
              (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def sniff(head):
    """Image type from the file's first bytes; Hermes cache names do not always carry a true extension."""
    for magic, mime in SIGNATURES:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def load_images(paths):
    """Read and check every image before anything uploads, so one bad file sends nothing."""
    paths = list(paths or [])
    if len(paths) > MAX_IMAGES:
        raise BossError("usage", f"at most {MAX_IMAGES} images per message", "send the rest in another message", 1)
    images = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        if size is None:
            raise BossError("usage", f"image {path} is not a readable file", "check the path", 1)
        if not 0 < size <= MAX_BYTES:
            raise BossError("usage", f"image {path.name} is {size} bytes; the limit is 1..{MAX_BYTES}",
                            "shrink or crop it, then retry", 1)
        data = path.read_bytes()
        mime = sniff(data[:12])
        if mime is None:
            raise BossError("usage", f"image {path.name} is not png, jpeg, gif or webp", "convert it, then retry", 1)
        images.append({"name": path.name, "mimeType": mime, "sizeBytes": len(data), "data": data})
    return images


def upload(client, images):
    """Prepare, upload and return T3 attachment records; discard every pending upload if one fails."""
    attachments = []
    try:
        for image in images:
            meta = {k: image[k] for k in ("name", "mimeType", "sizeBytes")}
            prepared = client.call("t3_attachment_prepare_upload", upload={"type": "image", **meta})
            if not prepared.get("attachmentId") or not prepared.get("relativeUrl"):
                raise BossError("protocol", "t3_attachment_prepare_upload returned no upload target")
            attachments.append({"type": "image", "id": prepared["attachmentId"], **meta})
            client.upload(prepared["relativeUrl"], image["data"], image["mimeType"])
    except BossError:
        for attachment in attachments:
            try:
                client.call("t3_attachment_discard", attachmentId=attachment["id"])
            except BossError:
                pass  # pending uploads also expire on their own
        raise
    return attachments


def journal(images):
    """What the ledger keeps about images: never the bytes."""
    return [{k: image[k] for k in ("name", "mimeType", "sizeBytes")} for image in images]
