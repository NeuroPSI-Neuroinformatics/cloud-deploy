A simple tool for deploying Docker containers in the cloud.

Currently supports DigitalOcean.

```
Usage: cld [OPTIONS] COMMAND [ARGS]...

Options:
  --debug
  --help   Show this message and exit.

Commands:
  bootstrap  Set-up the development and build environment.
  build      Build a Docker image locally and push to...
  database   Sub-command for managing database services.
  launch     Launch a new service.
  log        Display the log for a given service.
  node       Sub-command for managing server nodes.
  redeploy   Redeploy a running service.
  services   Display a list of services.
  terminate  Terminate a given service.
```