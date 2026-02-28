"""Windows stub for resource — no-op system resource limits."""
RUSAGE_SELF = 0
RUSAGE_CHILDREN = -1
RLIMIT_NOFILE = 7


class struct_rusage:
    ru_utime = 0.0
    ru_stime = 0.0
    ru_maxrss = 0
    ru_ixrss = 0
    ru_idrss = 0
    ru_isrss = 0
    ru_minflt = 0
    ru_majflt = 0
    ru_nswap = 0
    ru_inblock = 0
    ru_oublock = 0
    ru_msgsnd = 0
    ru_msgrcv = 0
    ru_nsignals = 0
    ru_nvcsw = 0
    ru_nivcsw = 0


def getrusage(who=RUSAGE_SELF):
    return struct_rusage()


def getrlimit(resource):
    return (1024, 1024)


def setrlimit(resource, limits):
    pass


def getpagesize():
    return 4096
