import os
if os.environ.get("BR_COV_DIR") and os.environ.get("BR_TEST") and os.environ.get("BR_SUBPROC", "1") == "1":
    import sys, atexit
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import brtrace
    brtrace.start(os.getcwd())
    brtrace.phase("test")
    atexit.register(lambda: brtrace.dump(os.path.join(os.environ["BR_COV_DIR"], os.environ["BR_TEST"] + f".sub{os.getpid()}.json")))
