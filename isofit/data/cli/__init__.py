import importlib
import pkgutil

from isofit.data import env
from isofit.data.download import cli

# Auto-discovers the submodules of isofit.data.cli
Modules = {
    name: importlib.import_module(f".{name}", __spec__.name)
    for imp, name, _ in pkgutil.iter_modules(__path__)
}


@cli.download.command(name="all")
@cli.update
@cli.check
@cli.validate
def download_all(update_, check, validate_):
    """\
    Downloads all ISOFIT extra dependencies to the locations specified in the isofit.ini file using latest tags and versions
    """
    pad = "=" * 16

    for i, module in enumerate(Modules.values()):
        if update_:
            print(f"{pad} Beginning update {i+1} of {len(Modules)} {pad}")
            module.update(check)

        elif validate_:
            print(f"{pad} Beginning validation {i+1} of {len(Modules)} {pad}")
            module.validate()

        else:
            print(f"{pad} Beginning download {i+1} of {len(Modules)} {pad}")
            module.download()

        print()

    print("Finished all processes")


def env_validate(keys, **kwargs):
    """
    Utility function for the `env` object to quickly validate specific dependencies

    Parameters
    ----------
    keys : list
        List of validator functions to call
    """
    error = kwargs.get("error", print)

    # Turn off checking for updates when using this function by default
    # This makes env.path less verbose
    kwargs["checkUpdate"] = kwargs.get("checkUpdate", False)

    all_valid = True
    for key in keys:
        module = Modules.get(key)
        if module is None:
            error(f"Product not found: {key}")
            all_valid = False
        else:
            all_valid &= module.validate(**kwargs)

    return all_valid


env.validate = env_validate
