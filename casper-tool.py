#!/usr/bin/env python3

from datetime import datetime, timedelta
import os
import subprocess

import click
import shutil
import toml
import yaml
from pathlib import Path
from itertools import chain


#: The port the node is reachable on.
NODE_PORT = 34553


@click.group()
@click.option(
    "-b",
    "--basedir",
    help="casper-node source code base directory",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
    default=os.path.join(os.path.dirname(__file__), "..", "casper-node"),
)
@click.option(
    "-l",
    "--launcher",
    help="casper-node-launcher source code base directory",
    type=click.Path(exists=True, dir_okay=True, file_okay=False, readable=True),
    default=os.path.join(os.path.dirname(__file__), "..", "casper-node-launcher"),
)
@click.option(
    "--casper-client",
    help="path to casper client binary (compiled from basedir by default)",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    default="../casper-node/target/release/casper-client",
)
@click.option(
    "-p",
    "--production",
    is_flag=True,
    help="Use production chainspec template instead of dev/local",
)
@click.option(
    "-c",
    "--config-template",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Node configuration template to use",
)
@click.option(
    "-C",
    "--chainspec-template",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Chainspec template to use",
)
@click.pass_context
def cli(
    ctx,
    basedir,
    launcher,
    production,
    chainspec_template,
    config_template,
    casper_client,
):
    """Casper Network creation tool

    Can be used to create new casper-labs chains with automatic validator setups. Useful for testing."""
    obj = {}
    if chainspec_template:
        obj["chainspec_template"] = chainspec_template
    elif production:
        obj["chainspec_template"] = os.path.join(
            basedir, "resources", "production", "chainspec.toml"
        )
    else:
        obj["chainspec_template"] = os.path.join(
            basedir, "resources", "local", "chainspec.toml.in"
        )

    if config_template:
        obj["config_template"] = chainspec_template
    elif production:
        obj["config_template"] = os.path.join(
            basedir, "resources", "production", "config.toml"
        )
    else:
        obj["config_template"] = os.path.join(
            basedir, "resources", "local", "config.toml"
        )

    if casper_client:
        obj["casper_client_argv0"] = [casper_client]
    else:
        obj["casper_client_argv0"] = [
            "cargo",
            "run",
            "--quiet",
            "--manifest-path={}".format(os.path.join(basedir, "client", "Cargo.toml")),
            "--",
        ]

    obj["casper-node-bin"] = \
            os.path.join(basedir, "target", "release", "casper-node")
    obj["casper-node-launcher-bin"] = \
            os.path.join(launcher, "target", "release", "casper-node-launcher")

    ctx.obj = obj
    return


@cli.command("create-network")
@click.pass_obj
@click.argument("target-path", type=click.Path(exists=False, writable=True), default="artifacts/chain-1")
@click.option(
    "-k",
    "--hosts-file",
    help="Parse an hosts.yaml file, using all.children.validators for set of known nodes",
    default="aws-hosts.yaml"
)
@click.option(
    "-n",
    "--network-name",
    help="The network name (also set in chainspec), defaults to output directory name",
)
@click.option(
    "-g",
    "--genesis-in",
    help="Number of seconds from now until Genesis",
    default=300,
    type=int,
)
@click.option(
    "-v",
    "--node-version",
    type=str,
    help="semver with underscores e.g. 1_0_0",
    default="1_0_0"
)
def create_network(
    obj,
    target_path,
    hosts_file,
    network_name,
    genesis_in,
    node_version,
):
    if not network_name:
        network_name = os.path.basename(os.path.join(target_path))

    # Create the network output directories.
    show_val("Output path", target_path)

    nodes_path = \
            os.path.join(target_path, "nodes")

    staging_path = os.path.join(target_path, "staging")
    bin_path = \
            os.path.join(staging_path, "bin")
    bin_version_path = \
            os.path.join(staging_path, "bin", node_version)
    config_path = \
            os.path.join(staging_path, "config")

    # Staging directories for config, chain
    show_val("Node version", node_version)

    Path(nodes_path).mkdir(parents=True)
    Path(bin_path).mkdir(parents=True)
    Path(bin_version_path).mkdir(parents=True)
    Path(config_path).mkdir(parents=True)

    # Update chainspec values.
    chainspec = create_chainspec(
        obj["chainspec_template"], network_name, genesis_in
    )

    # Dump chainspec into staging dir
    chainspec_path = os.path.join(config_path, "chainspec.toml")
    toml.dump(chainspec, open(chainspec_path, "w"))
    show_val("Chainspec", chainspec_path)

    # Copy casper-node into bin/VERSION/ staging dir
    node_bin_path = os.path.join(bin_version_path, "casper-node")
    shutil.copyfile(obj["casper-node-bin"], node_bin_path)
    os.chmod(node_bin_path, 0o744)

    # Copy casper-node into bin/ staging dir
    launcher_bin_path = os.path.join(bin_path, "casper-node-launcher")
    shutil.copyfile(obj["casper-node-launcher-bin"], launcher_bin_path)
    os.chmod(launcher_bin_path, 0o744)

    # Load validators from ansible yaml inventory
    hosts = yaml.load(open(hosts_file), Loader=yaml.FullLoader)

    # Setup each node, collecting all pubkey hashes.
    show_val("Node config template", obj["config_template"])

    validator_nodes = list(hosts["all"]["children"]["validators"]["hosts"].keys())
    bootstrap_nodes = list(hosts["all"]["children"]["bootstrap"]["hosts"].keys())
    zero_weight_nodes = list(hosts["all"]["children"]["zero_weight"]["hosts"].keys())

    bootstrap_keys = list()
    validator_keys = list()
    zero_weight_keys = list()

    for public_address in bootstrap_nodes:
        key_path = os.path.join(nodes_path, public_address, "etc", "casper", "keys")
        account = generate_account_key(key_path, public_address, obj)
        generate_node(bootstrap_nodes, obj, nodes_path, node_version, public_address)
        validator_keys.append(account)

    for public_address in validator_nodes:
        key_path = os.path.join(nodes_path, public_address, "etc", "casper", "keys")
        account = generate_account_key(key_path, public_address, obj)
        generate_node(
            ["{}:{}".format(n, NODE_PORT) for n in validator_nodes if n != public_address],
            obj, nodes_path, node_version, public_address)
        validator_keys.append(account)

    for public_address in zero_weight_nodes:
        key_path = os.path.join(nodes_path, public_address, "etc", "casper", "keys")
        account = generate_account_key(key_path, public_address, obj)
        generate_node(
            ["{}:{}".format(n, NODE_PORT) for n in bootstrap_nodes + validator_nodes],
            obj, nodes_path, node_version, public_address)
        zero_weight_keys.append(account)

    faucet_path = os.path.join(staging_path, "faucet")
    faucet_key = generate_account_key(faucet_path, "faucet", obj)

    accounts_path = os.path.join(config_path, "accounts.csv")
    # Copy accounts.csv into staging dir
    create_accounts_csv(open(accounts_path, "w"), faucet_key, bootstrap_keys + validator_keys, zero_weight_keys)

    for public_address in bootstrap_nodes + validator_nodes + zero_weight_nodes:
        node_path = os.path.join(nodes_path, public_address)

        # copy the bin and chain into each node's versioned fileset
        node_var_lib_casper = os.path.join(node_path, "var", "lib", "casper")
        node_bin_path = \
                os.path.join(node_var_lib_casper, "bin")
        Path(node_var_lib_casper).mkdir(parents=True)
        shutil.copytree(bin_path, node_bin_path)

        # should already exist
        node_config_path = \
                os.path.join(node_path, "etc", "casper", node_version)

        node_key_path = os.path.join(node_path, "etc", "casper", "keys")

        # copy the faucet's secret_key.pem into each node's config
        faucet_target_path = os.path.join(node_key_path, "faucet")
        Path(faucet_target_path).mkdir(parents=True)
        shutil.copyfile(
            os.path.join(faucet_path, "secret_key.pem"),
            os.path.join(faucet_target_path, "secret_key.pem")
        )

        for filename in os.listdir(config_path):
            shutil.copyfile(
                os.path.join(config_path, filename),
                os.path.join(node_config_path, filename)
            )


def generate_account_key(key_path, public_address, obj):
    run_client(obj["casper_client_argv0"], "keygen", key_path)
    pubkey_hex = open(os.path.join(key_path, "public_key_hex")).read().strip()
    return pubkey_hex


def generate_node(known_addresses, obj, nodes_path, node_version, public_address):
    node_path = os.path.join(nodes_path, public_address)
    node_config_path = \
        os.path.join(node_path, "etc", "casper", node_version)
    Path(node_config_path).mkdir(parents=True)
    config = toml.load(open(obj["config_template"]))
    config["node"]["chainspec_config_path"] = "chainspec.toml"
    config["consensus"]["secret_key_path"] = os.path.join("..", "keys", "secret_key.pem")
    # add faucet to the `faucet` subfolder in keys
    config["logging"]["format"] = "json"
    config["network"]["public_address"] = "{}:{}".format(public_address, NODE_PORT)
    config["network"]["bind_address"] = "0.0.0.0:{}".format(NODE_PORT)
    config["network"]["known_addresses"] = ["{}:{}".format(n, NODE_PORT) for n in known_addresses]
    # Setup for volume operation.
    storage_path = "/storage/{}".format(public_address)
    config["storage"]["path"] = storage_path
    config["consensus"]["unit_hashes_folder"] = storage_path
    toml.dump(config, open(os.path.join(node_config_path, "config.toml", ), "w"))


def create_chainspec(template, network_name, genesis_in):
    """Creates a new chainspec from a template.
    `contract_path` must be a dictionary mapping the keys of `CONTRACTS` to relative or absolute
    paths to be put into the new chainspec.
    Returns a dictionary that can be serialized using `toml`.
    """
    show_val("Chainspec template", template)
    chainspec = toml.load(open(template))

    show_val("Chain name", network_name)
    genesis_timestamp = (datetime.utcnow() + timedelta(seconds=genesis_in)).isoformat(
        "T"
    ) + "Z"
    show_val("Genesis", "{} (in {} seconds)".format(genesis_timestamp, genesis_in))
    chainspec["genesis"]["name"] = network_name
    chainspec["genesis"]["timestamp"] = genesis_timestamp
    chainspec["genesis"]["accounts_path"] = "accounts.csv"
    return chainspec


def create_node(
    public_address, client_argv0, config_template, node_path, validators
):
    """Create a node configuration inside a network.
    Paths are assumed to be set up using `create_chainspec`.
    Returns the nodes public key as a string."""
    # Generate a key
    key_path = os.path.join(node_path, "keys")
    run_client(client_argv0, "keygen", key_path)

    config = toml.load(open(config_template))
    config["node"]["chainspec_config_path"] = "chainspec.toml"
    config["consensus"]["secret_key_path"] = os.path.join(
        os.path.relpath(key_path, node_path), "secret_key.pem"
    )
    config["logging"]["format"] = "json"
    # Set the public address to `casper-node-XX`, which will resolve to the internal
    # network IP, and use the automatic port detection by setting `:0`.
    config["network"]["public_address"] = "{}:{}".format(public_address, NODE_PORT)
    config["network"]["bind_address"] = "0.0.0.0:{}".format(NODE_PORT)
    config["network"]["known_addresses"] = [
        "{}:{}".format(n, NODE_PORT)
        for n in validators
    ]
    # Setup for volume operation.
    storage_path = "/storage/{}".format(public_address)
    config["storage"]["path"] = storage_path
    config["consensus"]["unit_hashes_folder"] = storage_path
    toml.dump(config, open(os.path.join(node_path, "config.toml", ), "w"))
    return open(os.path.join(key_path, "public_key_hex")).read().strip()


def create_accounts_csv(output_file, faucet, validators, zero_weight_ops):
    """
    :param output_file: accounts.csv
    :param faucet: public key of faucet account
    :param validators: public keys of validators with weight
    :param zero_weight_ops: public keys of zero weight operators
    :return: output_file will be an appropriately formatted csv
    """
    output_file.write("{},{},{}\n".format(faucet, 10**32, 0))

    for index, key_hex in enumerate(validators):
        motes = 10**32
        staking_weight = 10**13 + index
        output_file.write("{},{},{}\n".format(key_hex, motes, staking_weight))

    for key_hex in zero_weight_ops:
        motes = 10**32
        staking_weight = 0
        output_file.write("{},{},{}\n".format(key_hex, motes, staking_weight))


def run_client(argv0, *args):
    """Run the casper client, compiling it if necessary, with the given command-line args"""
    return subprocess.check_output(argv0 + list(args))


def show_val(key, value):
    """Auxiliary function to display a value on the terminal."""

    key = "{:>20s}".format(key)
    click.echo("{}:  {}".format(click.style(key, fg="blue"), value))


if __name__ == "__main__":
    cli()
