# -*- coding: utf-8 -*-

import os

from flask import current_app as app

from clustermgr.models import Server, AppConfiguration
from clustermgr.extensions import celery, wlogger, db
from clustermgr.core.remote import RemoteClient
from clustermgr.core.ldap_functions import ldapOLC
from clustermgr.core.olc import CnManager
from clustermgr.core.utils import ldap_encode


def run_command(tid, c, command, container=None):
    """Shorthand for RemoteClient.run(). This function automatically logs
    the commands output at appropriate levels to the WebLogger to be shared
    in the web frontend.

    Args:
        tid (string): task id of the task to store the log
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        command (string): the command to be run on the remote server
        container (string, optional): location where the Gluu Server container
            is installed. For standalone LDAP servers this is not necessary.

    Returns:
        the output of the command or the err thrown by the command as a string
    """
    if container == '/':
        container = None
    if container:
        command = 'chroot {0} /bin/bash -c "{1}"'.format(container,
                                                         command)

    wlogger.log(tid, command, "debug")
    cin, cout, cerr = c.run(command)
    output = ''
    if cout:
        wlogger.log(tid, cout, "debug")
        output += "\n" + cout
    if cerr:
        # For some reason slaptest decides to send success message as err, so
        if 'config file testing succeeded' in cerr:
            wlogger.log(tid, cerr, "success")
        else:
            wlogger.log(tid, cerr, "error")
        output += "\n" + cerr

    return output


def upload_file(tid, c, local, remote):
    """Shorthand for RemoteClient.upload(). This function automatically handles
    the logging of events to the WebLogger

    Args:
        tid (string): id of the task running the command
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        local (string): local location of the file to upload
        remote (string): location of the file in remote server
    """
    out = c.upload(local, remote)
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success')


def download_file(tid, c, remote, local):
    """Shorthand for RemoteClient.download(). This function automatically handles
    the logging of events to the WebLogger

    Args:
        tid (string): id of the task running the command
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        remote (string): location of the file in remote server
        local (string): local location of the file to upload
    """
    out = c.download(remote, local)
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success')


@celery.task(bind=True)
def setupMmrServer(self, server_id):
    tid = self.request.id
    server = Server.query.get(server_id)
    conn_addr = server.hostname
    app_config = AppConfiguration.query.first()

    # 1. Ensure that the server id is valid
    if not server:
        wlogger.log(tid, "Server is not on database", "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    if not server.gluu_server:
        chroot = '/'
    else:
        chroot = '/opt/gluu-server-' + app_config.gluu_version

    # 2. Make SSH Connection to the remote server
    wlogger.log(tid, "Making SSH connection to the server %s" %
                server.hostname)
    c = RemoteClient(server.hostname, ip=server.ip)
    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    # 3. For Gluu server, ensure that chroot directory is available
    if server.gluu_server:
        if c.exists(chroot):
            wlogger.log(tid, 'Checking if remote is gluu server', 'success')
        else:
            wlogger.log(tid, "Remote is not a gluu server.", "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

    # 3.1 Ensure the data directories are available
    accesslog_dir = '/opt/gluu/data/accesslog'
    if not c.exists(chroot + accesslog_dir):
        run_command(tid, c, "mkdir -p {0}".format(accesslog_dir), chroot)
        run_command(tid, c, "chown -R ldap:ldap {0}".format(accesslog_dir),
                    chroot)

    # 4. Ensure Openldap is installed on the server - TODO
    # 5. Upload symas-openldap.conf with remote access and slapd.d enabled
    syconf = os.path.join(chroot, 'opt/symas/etc/openldap/symas-openldap.conf')
    confile = os.path.join(app.root_path, "templates", "slapd",
                           "symas-openldap.conf")

    HOST_LIST = 'HOST_LIST="ldaps://127.0.0.1:1636/ ldaps://{0}:1636/"'.format(
        conn_addr)
    EXTRA_SLAPD_ARGS = 'EXTRA_SLAPD_ARGS="-F /opt/symas/etc/openldap/slapd.d"'

    vals = {'HOST_LIST': HOST_LIST,
            'EXTRA_SLAPD_ARGS': EXTRA_SLAPD_ARGS,
            }

    confile_content = open(confile).read()
    confile_content = confile_content.format(**vals)

    r = c.put_file(syconf, confile_content)

    if r[0]:
        wlogger.log(tid, 'symas-openldap.conf file uploaded', 'success')
    else:
        wlogger.log(tid, 'An error occured while uploading symas-openldap.conf'
                    ': {0}'.format(r[1]), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # 6. Generate OLC slapd.d
    wlogger.log(tid, "Convert slapd.conf to slapd.d OLC")
    run_command(tid, c, 'service solserver stop', chroot)
    run_command(tid, c, "rm -rf /opt/symas/etc/openldap/slapd.d", chroot)
    run_command(tid, c, "mkdir -p /opt/symas/etc/openldap/slapd.d", chroot)
    run_command(tid, c, "/opt/symas/bin/slaptest -f /opt/symas/etc/openldap/"
                "slapd.conf -F /opt/symas/etc/openldap/slapd.d", chroot)
    run_command(tid, c,
                "chown -R ldap:ldap /opt/symas/etc/openldap/slapd.d", chroot)

    # 7. Restart the solserver with the new OLC configuration
    wlogger.log(tid, "Restarting LDAP server with OLC configuration")
    log = run_command(tid, c, "service solserver start", chroot)
    if 'failed' in log:
        wlogger.log(tid, "Couldn't restart solserver.", "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        run_command(tid, c, "service solserver start -d 1", chroot)
        return

    # 8. Connect to the OLC config
    ldp = ldapOLC('ldaps://{}:1636'.format(conn_addr), 'cn=config',
                  server.ldap_password)
    try:
        ldp.connect()
        wlogger.log(tid, 'Successfully connected to LDAPServer ', 'success')
    except Exception as e:
        wlogger.log(tid, "Connection to LDAPserver at port 1636 was failed:"
                    " {0}".format(e), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # 9. Set the server ID
    if ldp.setServerID(server.id):
        wlogger.log(tid, 'Setting Server ID: {0}'.format(server.id), 'success')
    else:
        wlogger.log(tid, "Stting Server ID failed: {0}".format(
            ldp.conn.result['description']), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # 10. Enable the syncprov and accesslog modules
    r = ldp.loadModules("syncprov", "accesslog")
    if r == -1:
        wlogger.log(
            tid, 'Syncprov and accesslog modlues already exist', 'debug')
    else:
        if r:
            wlogger.log(
                tid, 'Syncprov and accesslog modlues were loaded', 'success')
        else:
            wlogger.log(tid, "Loading syncprov and accesslog failed: {0}".format(
                ldp.conn.result['description']), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return

    if not ldp.checkAccesslogDBEntry():
        if ldp.accesslogDBEntry(app_config.replication_dn, accesslog_dir):
            wlogger.log(tid, 'Creating accesslog entry', 'success')
        else:
            wlogger.log(tid, "Creating accesslog entry failed: {0}".format(
                ldp.conn.result['description']), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return
    else:
        wlogger.log(tid, 'Accesslog entry already exists.', 'debug')

    # !WARNING UNBIND NECASSARY - I DON'T KNOW WHY.*****
    ldp.conn.unbind()
    ldp.conn.bind()

    if not ldp.checkSyncprovOverlaysDB1():
        if ldp.syncprovOverlaysDB1():
            wlogger.log(
                tid, 'SyncprovOverlays entry on main database was created',
                'success')
        else:
            wlogger.log(
                tid, "Creating SyncprovOverlays entry on main database failed:"
                " {0}".format(ldp.conn.result['description']), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return
    else:
        wlogger.log(
            tid, 'SyncprovOverlays entry on main database already exists.',
            'debug')

    if not ldp.checkSyncprovOverlaysDB2():
        if ldp.syncprovOverlaysDB2():
            wlogger.log(
                tid, 'SyncprovOverlay entry on accasslog database was created',
                'success')
        else:
            wlogger.log(
                tid, "Creating SyncprovOverlays entry on accasslog database"
                " failed: {0}".format(ldp.conn.result['description']), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return
    else:
        wlogger.log(
            tid, 'SyncprovOverlay entry on accasslog database already exists.',
            'debug')

    if not ldp.checkAccesslogPurge():
        if ldp.accesslogPurge():
            wlogger.log(tid, 'Creating accesslog purge entry', 'success')
        else:
            wlogger.log(tid, "Creating accesslog purge entry failed: {0}".format(
                ldp.conn.result['description']), "warning")

    else:
        wlogger.log(tid, 'Accesslog purge entry already exists.', 'debug')

    if ldp.setLimitOnMainDb(app_config.replication_dn):
        wlogger.log(
            tid, 'Setting size limit on main database for replicator user',
            'success')
    else:
        wlogger.log(tid, "Setting size limit on main database for replicator"
                    " user failed: {0}".format(ldp.conn.result['description']),
                    "warning")

    # 11. Add replication user to the o=gluu
    wlogger.log(tid, 'Creating replicator user: {0}'.format(
        app_config.replication_dn))

    adminOlc = ldapOLC('ldaps://{}:1636'.format(conn_addr),
                       'cn=directory manager,o=gluu', server.ldap_password)
    try:
        adminOlc.connect()
    except Exception as e:
        wlogger.log(
            tid, "Connection to LDAPserver as direcory manager at port 1636"
            " has failed: {0}".format(e), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    if adminOlc.addReplicatorUser(app_config.replication_dn,
                                  app_config.replication_pw):
        wlogger.log(tid, 'Replicator user created.', 'success')
    else:
        wlogger.log(tid, "Creating replicator user failed: {0}".format(
            adminOlc.conn.result), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # 12. Make this server to listen to all other providers
    wlogger.log(tid, "Adding Syncrepl config of other servers in the cluster")
    providers = Server.query.filter(Server.id.isnot(server.id)).all()
    for p in providers:
        if not p.mmr:
            continue
        paddr = p.ip if app_config.use_ip else p.hostname

        status = ldp.addProvider(
            p.id, "ldaps://{0}:1636".format(paddr), app_config.replication_dn,
            app_config.replication_pw)
        if status:
            wlogger.log(tid, 'Making LDAP of {0} listen to {1}'.format(
                server.hostname, p.hostname), 'success')
        else:
            wlogger.log(tid, 'Making {0} listen to {1} failed: {2}'.format(
                p.hostname, server.hostname, ldp.conn.result['description']),
                "warning")

        # 13. Make the other server listen to this server
        other = ldapOLC(p.hostname, "cn=config", p.ldap_password)
        try:
            other.connect()
        except Exception as e:
            wlogger.log("Couldn't connect to {0}. It will not be listening"
                        " to {1} for changes.".format(
                            p.hostname, server.hostname), "warning")
            continue
        saddr = server.ip if app_config.use_ip else server.hostname
        status = other.addProvider(server.id, "ldaps://{0}:1636".format(saddr),
                                   app_config.replication_dn,
                                   app_config.replication_pw)
        if status:
            wlogger.log(tid, 'Making LDAP of {0} listen to {1}'.format(
                p.hostname, server.hostname), 'success')
        else:
            wlogger.log(tid, 'Making {0} listen to {1} failed: {2}'.format(
                server.hostname, p.hostname, ldp.conn.result['description']),
                "warning")
        # Special case - if there are only two server enable mirror mode
        # in other server as well
        if len(providers) == 1:
            other.makeMirroMode()
        other.conn.unbind()

    # 14. Enable Mirrormode in the server
    if providers:
        if not ldp.checkMirroMode():
            if ldp.makeMirroMode():
                wlogger.log(tid, 'Enabling mirror mode', 'success')
            else:
                wlogger.log(tid, "Enabling mirror mode failed: {0}".format(
                    ldp.conn.result['description']), "warning")
        else:
            wlogger.log(tid, 'LDAP Server is already in mirror mode', 'debug')

    # 15. Set the mmr flag to True to indicate it has been configured
    server.mmr = True
    db.session.commit()

    wlogger.log(tid, "Deployment is successful")


@celery.task
def remove_provider(server_id):
    """Task to remove the syncrepl config of the given server from all other
    servers in the LDAP cluster.
    """
    appconfig = AppConfiguration.query.first()
    server = Server.query.get(server_id)
    recievers = Server.query.filter(Server.id.isnot(server_id)).all()
    for reciever in recievers:
        addr = reciever.hostname
        if appconfig.use_ip:
            addr = reciever.ip
        c = CnManager(addr, 1636, True, 'cn=config', reciever.ldap_password)
        c.remove_olcsyncrepl(server.id)
        c.close()
        # TODO monitor for failures and report or log it somewhere


@celery.task(bind=True)
def removeMultiMasterDeployement(self, server_id):
    app_config = AppConfiguration.query.first()
    server = Server.query.get(server_id)
    tid = self.request.id
    app_config = AppConfiguration.query.first()
    if not server.gluu_server:
        chroot = '/'
    else:
        chroot = '/opt/gluu-server-' + app_config.gluu_version

    wlogger.log(tid, "Making SSH connection to the server %s" %
                server.hostname)
    c = RemoteClient(server.hostname, ip=server.ip)

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    if server.gluu_server:
        # check if remote is gluu server
        if c.exists(chroot):
            wlogger.log(tid, 'Checking if remote is gluu server', 'success')
        else:
            wlogger.log(tid, "Remote is not a gluu server.", "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

    # symas-openldap.conf file exists
    if c.exists(os.path.join(chroot, 'opt/symas/etc/openldap/symas-openldap.conf')):
        wlogger.log(tid, 'Checking symas-openldap.conf exists', 'success')
    else:
        wlogger.log(tid, 'Checking if symas-openldap.conf exists', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # sldapd.conf file exists
    if c.exists(os.path.join(chroot, 'opt/symas/etc/openldap/slapd.conf')):
        wlogger.log(tid, 'Checking slapd.conf exists', 'success')
    else:
        wlogger.log(tid, 'Checking if slapd.conf exists', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # uplodading symas-openldap.conf file
    confile = os.path.join(app.root_path, "templates",
                           "slapd", "symas-openldap.conf")
    confile_content = open(confile).read()

    vals = {
        'HOST_LIST': 'HOST_LIST="ldaps://127.0.0.1:1636/"',
        'EXTRA_SLAPD_ARGS': 'EXTRA_SLAPD_ARGS=""',
    }

    confile_content = confile_content.format(**vals)

    r = c.put_file(os.path.join(
        chroot, 'opt/symas/etc/openldap/symas-openldap.conf'), confile_content)

    if r[0]:
        wlogger.log(tid, 'symas-openldap.conf file uploaded', 'success')
    else:
        wlogger.log(tid, 'An error occured while uploading symas-openldap.conf'
                    ': {0}'.format(r[1]), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    run_command(tid, c, "chown -R ldap:ldap /opt/symas/etc/openldap", chroot)

    # Restart the solserver with slapd.conf configuration
    wlogger.log(tid, "Restarting LDAP server with slapd.conf configuration")
    log = run_command(tid, c, "service solserver restart", chroot)
    if 'failed' in log:
        wlogger.log(tid,
                    "There seems to be some issue in restarting the server.",
                    "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return
    wlogger.log(tid, 'Deployment of Ldap Server was successfully removed')
    return True


@celery.task(bind=True)
def InstallLdapServer(self, ldap_info):
    tid = self.request.id

    wlogger.log(tid, "Making SSH connection to the server %s" %
                ldap_info['fqn_hostname'])
    c = RemoteClient(ldap_info['fqn_hostname'], ip=ldap_info['ip_address'])

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    # check if debian clone
    if c.exists('/usr/bin/dpkg'):
        wlogger.log(tid, 'Checking if /usr/bin/dpkg exists', 'success')
    else:
        wlogger.log(tid, '/usr/bin/dpkg nout found on this server', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    wlogger.log(tid, "Downloading and installing Symas Open-Ldap Server")
    cmd = "wget http://104.237.133.194/pkg/GLUU/UB14/symas-openldap-gluu.amd64_2.4.45-2_amd64.deb -O /tmp/symas-openldap-gluu.amd64_2.4.45-2_amd64.deb"
    cin, cout, cerr = c.run(cmd)

    if "‘/tmp/symas-openldap-gluu.amd64_2.4.45-2_amd64.deb’ saved" in cerr:
        wlogger.log(tid, 'Symas open-ldap package downloaded.', 'success')
    else:
        wlogger.log(tid, 'Downloading Symas open-ldap package failed', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    cmd = "dpkg -i /tmp/symas-openldap-gluu.amd64_2.4.45-2_amd64.deb"
    cin, cout, cerr = c.run(cmd)

    if "Setting up symas-openldap-gluu" in cout:
        wlogger.log(tid, 'Symas open-ldap package installed.', 'success')
    else:
        wlogger.log(tid, 'Installing Symas open-ldap package failed', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    wlogger.log(tid, "Creating ldap user and group")

    cmd = "adduser --system --no-create-home --group ldap"
    cin, cout, cerr = c.run(cmd)
    if "Adding system user" not in cout:
        wlogger.log(tid, "Can not add ldap user: {0}".format(
            cout.strip()), "warning")
        if "already exists. Exiting" not in cout:
            wlogger.log(tid, 'Creating ldap user failed', 'fail')
            wlogger.log(tid, "Ending server setup process.", "error")
            return

    wlogger.log(tid, "Uploading config file and gluu schemas")

    cmd = "mkdir -p /opt/gluu/schema/openldap/"
    c.run(cmd)
    if not c.exists('/opt/gluu/schema/openldap/'):
        wlogger.log(tid, 'Creating "/opt/gluu/schema/openldap/" failed',
                    'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    custom_schema_file = os.path.join(app.root_path, "templates",
                                      "slapd", "schema", "custom.schema")
    gluu_schema_file = os.path.join(
        app.root_path, "templates", "slapd", "schema", "gluu.schema")
    r1 = c.upload(custom_schema_file,
                  "/opt/gluu/schema/openldap/custom.schema")
    r2 = c.upload(gluu_schema_file, "/opt/gluu/schema/openldap/gluu.schema")
    err = ''
    if 'Upload successful.' not in r1:
        err += r1
    if 'Upload successful.' not in r2:
        err += r2
    if err:
        wlogger.log(
            tid, 'Uploading Gluu schema files failed: {0}'.format(err), 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return
    wlogger.log(tid, 'Gluu schema files uploaded', 'success')

    gluu_slapd_conf_file = os.path.join(
        app.root_path, "templates", "slapd", "slapd.conf.gluu")
    gluu_slapd_conf_file_content = open(gluu_slapd_conf_file).read()

    hashpw = ldap_encode(ldap_info["ldap_password"])

    gluu_slapd_conf_file_content = gluu_slapd_conf_file_content.replace(
        "{#ROOTPW#}", hashpw)

    r = c.put_file("/opt/symas/etc/openldap/slapd.conf",
                   gluu_slapd_conf_file_content)

    if r[0]:
        wlogger.log(tid, 'slapd.conf file uploaded', 'success')
    else:
        wlogger.log(tid, 'An error occured while uploading slapd.conf.conf:'
                    ' {0}'.format(r[1]), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    wlogger.log(tid, 'Gluu slapd.conf file uploaded', 'success')

    cmd = "mkdir -p /opt/gluu/data/main_db"
    c.run(cmd)
    if not c.exists('/opt/gluu/data/main_db'):
        wlogger.log(tid, 'Creating "/opt/gluu/data/main_db" failed', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    cmd = "mkdir -p /opt/gluu/data/site_db"
    c.run(cmd)
    if not c.exists('/opt/gluu/data/site_db'):
        wlogger.log(tid, 'Creating "/opt/gluu/data/site_db" failed', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    wlogger.log(
        tid, 'Directories "/opt/gluu/data/main_db" and "/opt/gluu/data/site_db" were created', 'success')

    run_command(
        tid, c, "chown -R {0}.{1} /opt/gluu/data/".format(
            ldap_info["ldap_user"], ldap_info["ldap_group"]))

    run_command(tid, c, "mkdir -p /var/symas/run/")

    run_command(
        tid, c, "chown -R {0}.{1} /var/symas".format(
            ldap_info["ldap_user"], ldap_info["ldap_group"]))

    run_command(tid, c, "mkdir -p /etc/certs/")

    wlogger.log(tid, "Generating Certificate")
    cmd = "/usr/bin/openssl genrsa -des3 -out /etc/certs/openldap.key.orig -passout pass:{0} 2048".format(
        ldap_info["ldap_password"])

    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(cmd)
    wlogger.log(tid, cin + cout + cerr, "debug")

    cmd = "/usr/bin/openssl rsa -in /etc/certs/openldap.key.orig -passin pass:{0} -out /etc/certs/openldap.key".format(
        ldap_info["ldap_password"])
    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(cmd)
    wlogger.log(tid, cin + cout + cerr, "debug")

    subj = '/C={0}/ST={1}/L={2}/O={3}/CN={4}/emailAddress={5}'.format(
        ldap_info['countryCode'], ldap_info['state'], ldap_info['city'],
        ldap_info['orgName'], ldap_info['fqn_hostname'],
        ldap_info['admin_email'])

    cmd = '/usr/bin/openssl req -new -key /etc/certs/openldap.key -out /etc/certs/openldap.csr -subj {0}'.format(
        subj)

    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(cmd)
    if cout.strip() + cerr.strip():
        wlogger.log(tid, cin + cout + cerr, "debug")

    cmd = "/usr/bin/openssl x509 -req -days 365 -in /etc/certs/openldap.csr -signkey /etc/certs/openldap.key -out /etc/certs/openldap.crt"
    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(cmd)
    wlogger.log(tid, cin + cout + cerr, "debug")

    cmd = "cat /etc/certs/openldap.crt >> /etc/certs/openldap.pem && cat /etc/certs/openldap.key >> /etc/certs/openldap.pem"
    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(cmd)
    if cout.strip() + cerr.strip():
        wlogger.log(tid, cin + cout + cerr, "debug")

    run_command(tid, c, "chown -R {0}.{1} /etc/certs".format(
        ldap_info["ldap_user"], ldap_info["ldap_group"]))

    # uplodading symas-openldap.conf file
    confile = os.path.join(app.root_path, "templates",
                           "slapd", "symas-openldap.conf")
    confile_content = open(confile).read()

    vals = {
        'HOST_LIST': 'HOST_LIST="ldaps://127.0.0.1:1636/"',
        'EXTRA_SLAPD_ARGS': 'EXTRA_SLAPD_ARGS=""',
        'SLAPD_GROUP': ldap_info["ldap_user"],
        'SLAPD_USER': ldap_info["ldap_group"],
    }

    confile_content = confile_content.format(**vals)

    r = c.put_file('/opt/symas/etc/openldap/symas-openldap.conf',
                   confile_content)

    if r[0]:
        wlogger.log(tid, 'symas-openldap.conf file uploaded', 'success')
    else:
        wlogger.log(tid, 'An error occured while uploading symas-openldap.conf'
                    ': {0}'.format(r[1], 'fail'))
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    wlogger.log(tid, "Satring Symas Open-Ldap Server")
    log = run_command(tid, c, "service solserver restart")
    if 'failed' in log:
        wlogger.log(
            tid, "There seems to be some issue in restarting the server.",
            "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    ldps = Server()
    ldps.hostname = ldap_info["fqn_hostname"]
    ldps.ip = ldap_info["ip_address"]
    ldps.ldap_password = ldap_info["ldap_password"]
    db.session.add(ldps)
    db.session.commit()
