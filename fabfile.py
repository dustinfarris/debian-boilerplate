import crypt
import random
import string
import sys
from os.path import expanduser

from invoke import Responder, task
from invocations.console import confirm


BLUE = "\033[0;34m"
RESET = "\033[0m"
GRAY = "\033[0;37m"

def status(message):
    print("\n\n%s%s%s...%s\n" % (BLUE, message, GRAY, RESET))


load_ssh_configs = False


local_home = expanduser("~")


admin_password = None


def run_as(c, command, user):
    # This complicated wrapper allows us to run a command as a user as if the
    # user had logged in (getting all the .bashrc stuff, environment variables, etc)
    command = command.replace('$', '\\$').replace('"', '\\"')
    c.run('sudo -i -u {user} /bin/bash -lic "{command}"'.format(user=user, command=command), pty=True)


def prompt(text, default=None):
    if default:
        text = "%s [%s]: " % (text, default)
    else:
        text = "%s: " % text
    response = input(text)
    if not response:
        return default
    return response


def authenticate(c):
    password = input("Root password: ")
    c.connect_kwargs.update({"password": password})


def scaffolding(c, server_name):
    status("installing essentials")
    c.run("apt-get update -q")
    c.run("apt-get upgrade -qy")
    c.run("apt-get install tmux git-core -qy")

    status("setting hostname")
    c.run("echo '%s' > /etc/hostname" % server_name)
    c.run("hostname -F /etc/hostname")
    c.run("echo \"`ifconfig eth0|grep inet|awk {'print $2'}|cut -d':' -f2`  %s\" >> /etc/hosts" % server_name)

    status("setting time zone")
    c.run("ln -sfn /usr/share/zoneinfo/America/New_York /etc/localtime")

    status("configuring shell")
    c.put("files/bash.bashrc", "/etc/bash.bashrc")
    c.put("files/root.bashrc", "/root/.bashrc")
    c.put("files/skel.bashrc", "/etc/skel/.bashrc")
    c.run("touch /etc/skel/.hushlogin")

    status("configuring editor")
    c.run("update-alternatives --set editor /usr/bin/vim.basic")

    status("removing unnecessary packages")
    c.run("apt-get purge rpcbind -qy")
    c.run("apt-get autoremove -qy")


def harden(c):
    status("restricting sudo usage")
    c.put("files/sudoers", "/etc/sudoers")

    status("restricting SSH access")
    c.put("files/sshd_config", "/etc/ssh/sshd_config")

    status("setting up firewall")
    c.put("files/iptables_v4", "/tmp/v4")
    c.put("files/iptables_v6", "/tmp/v6")
    c.run("iptables-restore < /tmp/v4")
    c.run("ip6tables-restore < /tmp/v6")
    ipv4_responder = Responder(pattern=r"Save current IPv4 rules?", response="y\n")
    ipv6_responder = Responder(pattern=r"Save current IPv6 rules?", response="y\n")
    c.run(
      "DEBIAN_FRONTEND=readline apt-get install iptables-persistent -qy",
      watchers=[ipv4_responder, ipv6_responder],
      pty=True
    )


def create_admin_user(c, server_name):
    status("creating admin user")
    # Keep track of the password in case the build fails somewhere
    # down the line
    global admin_password

    # Generate a password and encrypted version
    characters = string.punctuation + string.ascii_letters + string.digits
    password_size = 50
    admin_password = ''.join(random.SystemRandom().choice(characters) for _ in range(password_size))
    salt_characters = string.ascii_letters + string.digits
    salt = ''.join(random.SystemRandom().choice(salt_characters) for _ in range(3))
    admin_crypt = crypt.crypt(admin_password, salt)

    # Create admin user
    c.run("useradd admin -Um -s /bin/bash -p %s" % admin_crypt)

    # Create a SSH key
    c.sudo(
      "ssh-keygen -t rsa -f /home/admin/.ssh/id_rsa -C 'admin@%s' -q -N ''" % server_name,
      user="admin"
    )

    # Copy this machine's public key
    c.put(
      "{local_home}/.ssh/id_rsa.pub".format(local_home=local_home),
      "/home/admin/.ssh/authorized_keys"
    )
    c.run("chown admin: /home/admin/.ssh/authorized_keys")


def create_worker_user(c, user, server_name, project_name):
    status("creating %s user" % user)
    # Create user
    c.run('useradd --system --shell=/bin/bash --home=/var/{user} --create-home {user}'.format(user=user))

    # Create a SSH key
    c.sudo(
      "ssh-keygen -t rsa -f /var/{user}/.ssh/id_rsa -C '{user}@%{server}' -q -N ''".format(
        server=server_name,
        user=user,
      ),
      user=user
    )

    # Copy this machine's public key
    c.put(
      "{local_home}/.ssh/id_rsa.pub".format(local_home=local_home),
      "/var/{user}/.ssh/authorized_keys".format(user=user)
    )
    c.run("chown {user}: /var/{user}/.ssh/authorized_keys".format(user=user))

    # Create a CI public/private key pair
    c.sudo(
      "ssh-keygen -t rsa -f /var/{user}/ci_key -C 'CI@{project_name}' -q -N ''".format(
        user=user,
        project_name=project_name,
      ),
      user=user
    )
    c.run("cat /var/{user}/ci_key.pub >> /var/{user}/.ssh/authorized_keys".format(user=user))


def install_postgres(c):
    status("installing postgres")
    c.run("apt-get install postgresql -qy")

    status("creating postgres superuser")
    c.sudo("createuser -s admin", user="postgres")


def install_nginx(c):
    status("getting nginx sources")
    # Debian provides nginx 1.6, but we need at least 1.9 for
    # http/2 support. Add nginx debian sources and public key.
    c.run("echo \"deb http://nginx.org/packages/debian/ `lsb_release -cs` nginx\" >> /etc/apt/sources.list")
    c.run("echo \"deb-src http://nginx.org/packages/debian/ `lsb_release -cs` nginx\" >> /etc/apt/sources.list")
    c.run("curl http://nginx.org/keys/nginx_signing.key | apt-key add -")
    c.run("apt-get update")

    status("installing nginx")
    c.run("apt-get install nginx -qy")

    status("updating nginx parameters")
    c.put("files/nginx-proxy-params", "/etc/nginx/proxy_params")
    c.put("files/nginx-ssl-params", "/etc/nginx/ssl_params")


def install_letsencrypt(c):
    status("generating DH params")
    # Generate a prime number for DH to use when creating its key
    c.run("openssl dhparam -out /etc/ssl/certs/dhparam.pem 2048")

    # Get letsencrypt!
    status("installing letsencrypt")
    c.run("git clone https://github.com/letsencrypt/letsencrypt /opt/letsencrypt")
    c.run("mkdir -p /etc/letsencrypt/configs")
    c.run("mkdir -m775 -p /var/opt/letsencrypt")
    c.run("chgrp admin /var/opt/letsencrypt")
    c.run("mkdir -m775 -p /var/log/letsencrypt")
    c.run("chgrp admin /var/log/letsencrypt")


def create_phoenix_vhost(c, domain_name):
    status("creating nginx vhost for %s" % domain_name)
    c.put(
      "files/phoenix-nginx.conf",
      "/etc/nginx/conf.d/%s.conf" % domain_name
    )
    c.run(
      "sed -i 's/DOMAIN_NAME/{domain}/g' /etc/nginx/conf.d/{domain}.conf".format(
        domain=domain_name
      )
    )
    c.run("systemctl reload nginx")


def create_ssl_cert(c, domain_name, email):
    status("creating SSL certificate for %s" % domain_name)
    # Configure for this domain
    c.put(
      "files/letsencrypt",
      "/etc/letsencrypt/configs/{domain}.conf".format(
        domain=domain_name
      )
    )
    c.run(
      "sed -i 's/DOMAIN_NAME/{domain}/g' /etc/letsencrypt/configs/{domain}.conf".format(
        domain=domain_name
      )
    )
    c.run(
      "sed -i 's/USER_EMAIL/{email}/g' /etc/letsencrypt/configs/{domain}.conf".format(
        email=email, domain=domain_name
      )
    )

    # Get a new cert from letsencrypt
    c.run(
      "/opt/letsencrypt/letsencrypt-auto --config /etc/letsencrypt/configs/{domain}.conf certonly".format(
        domain=domain_name
      )
    )

    # Assume nginx has been configured for this domain with
    # the included script. Adjust for SSL.
    c.run(
      "sed -i 's/# ssl_cert/ssl_cert/g' /etc/nginx/conf.d/{domain}.conf".format(
        domain=domain_name
      )
    )
    c.run("systemctl reload nginx")

    # Set up 90-day renewal
    c.put(
      "files/renew-letsencrypt.sh",
      "/usr/local/bin/renew-{domain}-cert.sh".format(domain=domain_name)
    )
    c.run(
      "sed -i 's/DOMAIN_NAME/{domain}/g' /usr/local/bin/renew-{domain}-cert.sh".format(
        domain=domain_name
      )
    )
    c.run(
      "(crontab -l; echo '0 0 1 JAN,MAR,MAY,JUL,SEP,NOV * /usr/local/bin/renew-{domain}-cert.sh') | crontab".format(
        domain=domain_name
      )
    )


def phoenix_server(c):
    # Configuration
    project_name = prompt("Project name")
    environment = prompt("Environment", default="prod")
    server_name = prompt(
      "Server name",
      default="{project_name}-{environment}".format(
        project_name=project_name,
        environment=environment,
      )
    )
    domain_name = prompt(
      "Domain name",
      default="%s.com" % project_name if environment == "prod" else "%s.%s.com" % (environment, project_name)
    )
    email = prompt("Email (for letsencrypt)", default="admin@%s.com" % project_name)

    # Build server
    authenticate(c)
    scaffolding(c, server_name)
    create_admin_user(c, server_name)
    create_worker_user(c, "web", server_name, project_name)
    install_postgres(c)
    install_nginx(c)
    install_letsencrypt(c)
    create_phoenix_vhost(c, domain_name)
    harden(c)


def install_erlang_elixir(c, erlang_version, elixir_version, user):
    status("installing Erlang/Elixir dependencies")
    # Erlang requirements
    c.run('apt install -qy make autoconf m4 libncurses5-dev')
    # For building with wxWidgets
    c.run('apt install -qy libwxgtk3.0-dev libgl1-mesa-dev libglu1-mesa-dev libpng3')
    # For building ssl (libssh-4 libssl-dev zlib1g-dev)
    c.run('apt install -qy libssh-dev')
    # ODBC support (libltdl3-dev odbcinst1debian2 unixodbc)
    c.run('apt install -qy unixodbc-dev')
    # We will use asdf package manager for erlang and elixir
    # Note that this will compile erlang and elixir which can take a while
    status("downloading asdf")
    run_as(c, 'git clone https://github.com/asdf-vm/asdf.git ~/.asdf', user=user)
    # These have to go in .profile because automators may not use bash
    run_as(c, "echo '. $HOME/.asdf/asdf.sh' >> ~/.profile", user=user)
    run_as(c, "echo '. $HOME/.asdf/completions/asdf.bash' >> ~/.profile", user=user)
    # Install erlang and elixir
    run_as(c, "asdf plugin-add erlang https://github.com/asdf-vm/asdf-erlang.git", user=user)
    run_as(c, "asdf plugin-add elixir https://github.com/asdf-vm/asdf-elixir.git", user=user)
    status("installing Erlang")
    run_as(c, "asdf install erlang {version}".format(version=erlang_version), user=user)
    run_as(c, "asdf global erlang {version}".format(version=erlang_version), user=user)
    status("installing Elixir")
    run_as(c, "asdf install elixir {version}".format(version=elixir_version), user=user)
    run_as(c, "asdf global elixir {version}".format(version=elixir_version), user=user)
    # A couple necessities
    status("installing Hex")
    run_as(c, "mix local.hex --force", user=user)
    status("installing Rebar")
    run_as(c, "mix local.rebar --force", user=user)


def builder_server(c):
    project_name = prompt("Project name")
    server_name = prompt("Server name", default="%s-build" % project_name)
    erlang_version = prompt("Erlang version", default="19.3")
    elixir_version = prompt("Elixir version", default="1.4.4")

    authenticate(c)
    scaffolding(c, server_name)
    create_admin_user(c, server_name)
    create_worker_user(c, "builder", server_name, project_name)
    install_erlang_elixir(c, erlang_version, elixir_version, user="builder")
    harden(c)


def build(c, flavor):
    try:
        flavor(c)
    except:
        print("\n\nSomething went wrong.\n\n")
        raise
    else:
        print("\nYou should reboot the server now.\n\nEnjoy your life!\n\n")
    finally:
        if admin_password:
            print("\n\nADMIN PASSWORD\n\n%s\n\n" % admin_password)


@task
def create_phoenix(c):
    build(c, phoenix_server)


@task
def create_builder(c):
    build(c, builder_server)
