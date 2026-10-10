# Direct IP HTTPS edge

`ip_tls.py` configures only the host Nginx site for `79.141.74.15`. It expects
the deployment bootstrap to have installed Nginx and Certbot 5.4–5.x at
`/opt/novostroy-certbot/bin/certbot`. It does not install packages, edit
Compose, open firewall ports, or adopt/replace unrecognized Nginx or Certbot
files.

Run each phase on the reviewed Ubuntu 24.04 host through the authenticated
Teleport session. First inspect the host without changing it:

```sh
sudo python3 deploy/ip_tls.py preflight
```

The HTTP phase serves only ACME challenge files for the target IP; all other
requests to that IP receive 404. It starts Nginx if the deployment bootstrap
left it stopped, after checking the site configuration and listeners.

```sh
sudo python3 deploy/ip_tls.py stage-http --confirm-ip 79.141.74.15
sudo python3 deploy/ip_tls.py issue-staging --confirm-ip 79.141.74.15
sudo python3 deploy/ip_tls.py issue-production --confirm-ip 79.141.74.15
sudo python3 deploy/ip_tls.py enable-renewal --confirm-ip 79.141.74.15
```

The staging issuance is the external HTTP-01 reachability test. Do not proceed
to production if it fails. HTTPS remains unactivated until the app owner has
deployed the release and this local endpoint returns HTTP 200:

```sh
curl --fail http://127.0.0.1:8080/healthz
sudo python3 deploy/ip_tls.py activate-https --confirm-ip 79.141.74.15
```

Then verify from outside the VM with normal certificate validation (never
`--insecure`):

```sh
curl --fail https://79.141.74.15/healthz
```

`status` reports local phases and certificate checks; only the external request
confirms the public route and browser-trusted HTTPS. The certificate private
key stays under Certbot's root-owned lineage and is never printed by the
script.

The issuance flags follow the current Certbot CLI for IP SANs, preferred
`shortlived` profile, and webroot HTTP-01; renewal is scoped to this certificate
and periodically runs a staging dry-run before its timer is enabled. See the
[Certbot 5.8.0 CLI reference](https://eff-certbot.readthedocs.io/en/stable/man/certbot.html),
[Certbot renewal guidance](https://eff-certbot.readthedocs.io/en/stable/using.html),
and [Let's Encrypt challenge types](https://letsencrypt.org/docs/challenge-types/).
