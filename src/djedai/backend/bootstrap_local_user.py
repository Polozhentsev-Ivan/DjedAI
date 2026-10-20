"""CLI entrypoint kept separate from dependencies imported by HTTP routers."""

from djedai.backend.local_user import main

if __name__ == "__main__":
    main()
