import sys

if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a]
    if "--web" in argv:
        from flood_model.app import run_server

        raise SystemExit(run_server())
    from flood_model.flood_3d import main

    raise SystemExit(main([a for a in argv if a != "--web"] or None))
