"""python -m mikrobox: run the API server."""

import logging
import os

import uvicorn

from .api import create_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("MIKROBOX_LOG", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("MIKROBOX_HOST", "0.0.0.0"),
        port=int(os.environ.get("MIKROBOX_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
