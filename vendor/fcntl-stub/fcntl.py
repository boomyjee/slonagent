"""Windows stub for fcntl — no-op file locking."""
LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):
    pass


def fcntl(fd, cmd, arg=0):
    return 0


def ioctl(fd, request, arg=0, mutate_flag=True):
    return 0
