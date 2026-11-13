import uuid


class UuidIdGen:
    def new(self, kind: str) -> str:
        return f"{kind}_{uuid.uuid4().hex}"
