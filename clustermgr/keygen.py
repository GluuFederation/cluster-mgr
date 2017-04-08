import os
import shlex
import subprocess


def exec_cmd(cmd):
    args = shlex.split(cmd)
    popen = subprocess.Popen(args,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    stdout, stderr = popen.communicate()
    retcode = popen.returncode
    return stdout, stderr, retcode


def generate_jks(passwd, javalibs_dir, jks_path, exp=365, alg="RS512"):
    if os.path.exists(jks_path):
        os.unlink(jks_path)

    cmd = " ".join([
        "java", "-Dlog4j.defaultInitOverride=true",
        "-cp", "'{}/*'".format(javalibs_dir),
        "org.xdi.oxauth.util.KeyGenerator",
        "-algorithms", alg,
        "-dnname", "{!r}".format("CN=oxAuth CA Certificates"),
        "-expiration", "{}".format(exp),
        "-keystore", jks_path,
        "-keypasswd", passwd,
    ])
    return exec_cmd(cmd)
