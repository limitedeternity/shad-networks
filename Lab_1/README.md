```bash
$ PIPENV_VENV_IN_PROJECT=1 pipenv sync --dev
<...>
All dependencies are now up-to-date!

$ sudo su
# pipenv shell
Launching subshell in virtual environment...

(Lab_1) # python generate_topology.py --help
usage: generate_topology.py [-h] [--subnets SUBNETS]

options:
  -h, --help         show this help message and exit
  --subnets SUBNETS  amount of subnets (default: 3)
(Lab_1) # python generate_topology.py
name: static_routing
prefix: ''
mgmt:
    network: statics
    ipv4-subnet: 172.20.20.0/24
topology:
    nodes:
        R0:
            kind: linux
            image: quay.io/frrouting/frr:9.1.0
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.2
        R1:
            kind: linux
            image: quay.io/frrouting/frr:9.1.0
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.3
        R2:
            kind: linux
            image: quay.io/frrouting/frr:9.1.0
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.4
        PC0:
            kind: linux
            image: frrouting/frr-debian:latest
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.5
        PC1:
            kind: linux
            image: frrouting/frr-debian:latest
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.6
        PC2:
            kind: linux
            image: frrouting/frr-debian:latest
            binds:
            - ./.init/daemons:/etc/frr/daemons
            mgmt-ipv4: 172.20.20.7
    links:
    -   endpoints:
        - R0:eth1
        - R1:eth1
    -   endpoints:
        - R1:eth2
        - R2:eth1
    -   endpoints:
        - R2:eth2
        - R0:eth2
    -   endpoints:
        - PC0:eth1
        - R0:eth3
    -   endpoints:
        - PC1:eth1
        - R1:eth3
    -   endpoints:
        - PC2:eth1
        - R2:eth3
(Lab_1) # python generate_topology.py > topology.yml
(Lab_1) # clab deploy -t topology.yml
<...>
+---+------+--------------+-----------------------------+-------+---------+----------------+--------------+
| # | Name | Container ID |            Image            | Kind  |  State  |  IPv4 Address  | IPv6 Address |
+---+------+--------------+-----------------------------+-------+---------+----------------+--------------+
| 1 | PC0  | 15a8b9ae3681 | frrouting/frr-debian:latest | linux | running | 172.20.20.5/24 | N/A          |
| 2 | PC1  | eaa1866dfc0b | frrouting/frr-debian:latest | linux | running | 172.20.20.6/24 | N/A          |
| 3 | PC2  | 692f15866663 | frrouting/frr-debian:latest | linux | running | 172.20.20.7/24 | N/A          |
| 4 | R0   | 1d9ff82eeae7 | quay.io/frrouting/frr:9.1.0 | linux | running | 172.20.20.2/24 | N/A          |
| 5 | R1   | 3fff083106f1 | quay.io/frrouting/frr:9.1.0 | linux | running | 172.20.20.3/24 | N/A          |
| 6 | R2   | 4f4affcf5969 | quay.io/frrouting/frr:9.1.0 | linux | running | 172.20.20.4/24 | N/A          |
+---+------+--------------+-----------------------------+-------+---------+----------------+--------------+
(Lab_1) # python configure_nodes.py --help
usage: configure_nodes.py [-h] -t TOPOLOGY

options:
  -h, --help            show this help message and exit
  -t TOPOLOGY, --topology TOPOLOGY
                        path to topology.yml file
(Lab_1) # python configure_nodes.py -t topology.yml
---------
R1 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
eth0            up      default         172.20.20.3/24
eth1            up      default         192.168.1.2/24
eth2            up      default         192.168.2.2/24
eth3            up      default         172.25.2.2/24
lo              up      default         10.10.10.2/32

R1#
exit

---------
R0 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
clab-c920b2c1   down    default
eth0            up      default         172.20.20.2/24
eth1            up      default         192.168.1.1/24
eth2            up      default         192.168.3.1/24
eth3            up      default         172.25.1.1/24
lo              up      default         10.10.10.1/32

R0# exit

---------
PC2 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
eth0            up      default         172.20.20.7/24
eth1            up      default         172.25.3.4/24
lo              up      default

PC2#
exit

---------
R2 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
eth0            up      default         172.20.20.4/24
eth1            up      default         192.168.2.3/24
eth2            up      default         192.168.3.3/24
eth3            up      default         172.25.3.3/24
lo              up      default         10.10.10.3/32


R2#
exit

---------
PC1 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
eth0            up      default         172.20.20.6/24
eth1            up      default         172.25.2.3/24
lo              up      default

PC1#
exit

---------
PC0 configuration:
---------

Interface       Status  VRF             Addresses
---------       ------  ---             ---------
eth0            up      default         172.20.20.5/24
eth1            up      default         172.25.1.2/24
lo              up      default

PC0#
exit
```
