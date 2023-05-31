#!/usr/bin/env python3
"""Arrakis single-field pipeline"""
import os
from pathlib import Path
from typing import Any

import astropy.units as u
import configargparse
import pkg_resources
import yaml
from astropy.time import Time
from dask.distributed import Client, performance_report
from dask_jobqueue import SLURMCluster
from dask_mpi import initialize
from prefect import flow, task
from prefect_dask import DaskTaskRunner

from arrakis import (
    cleanup,
    cutout,
    frion,
    imager,
    linmos,
    makecat,
    rmclean_oncuts,
    rmsynth_oncuts,
)
from arrakis.logger import logger
from arrakis.utils import port_forward, test_db


# Defining tasks
cut_task = task(cutout.cutout_islands, name="Cutout")
linmos_task = task(linmos.main, name="LINMOS")
frion_task = task(frion.main, name="FRion")
cleanup_task = task(cleanup.main, name="Clean up")
rmsynth_task = task(rmsynth_oncuts.main, name="RM Synthesis")
rmclean_task = task(rmclean_oncuts.main, name="RM-CLEAN")
cat_task = task(makecat.main, name="Catalogue")
imager_task = task(imager.main, name="Imaging stage")

@flow(name="Imaging Arrakis data")
def process_imager(*args, **kwargs) -> None: 
    logger.info("Running the imager stage.")
    
    imager_task.submit(*args, **kwargs)
    
    return

@flow(name="Process the Spice")
def process_spice(
    args: configargparse.Namespac, host: str
) -> None:
    """Workflow to process the SPIRCE-RACS data

    Args:
        args (configargparse.Namespac): Configuration parameters for this run
        host (str): Host address of the mongoDB. 
    """
    
    previous_future = None
    previous_future = cut_task.submit(
            field=args.field,
            directory=args.datadir,
            host=host,
            username=args.username,
            password=args.password,
            verbose=args.verbose,
            pad=args.pad,
            stokeslist=["I", "Q", "U"],
            verbose_worker=args.verbose_worker,
            dryrun=args.dryrun,
        ) if not args.skip_cuts else previous_future
    
    previous_future = linmos_task.submit(
            field=args.field,
            datadir=args.datadir,
            host=host,
            holofile=args.holofile,
            username=args.username,
            password=args.password,
            yanda=args.yanda,
            stokeslist=["I", "Q", "U"],
            verbose=True,
            wait_for=[previous_future],
        ) if not args.skip_linmos else previous_future
    
        
    previous_future = cleanup_task.submit(
        datadir=args.datadir,
        stokeslist=["I", "Q", "U"],
        verbose=True,
        wait_for=[previous_future],
    ) if not args.skip_cleanup else previous_future
    
    previous_future = frion_task.submit(
        args.skip_frion,
        field=args.field,
        outdir=args.datadir,
        host=host,
        username=args.username,
        password=args.password,
        database=args.database,
        verbose=args.verbose,
        wait_for=[previous_future],
    ) if not args.skip_frion else previous_future
    
    previous_future = rmsynth_task.submit(
        field=args.field,
        outdir=args.datadir,
        host=host,
        username=args.username,
        password=args.password,
        dimension=args.dimension,
        verbose=args.verbose,
        database=args.database,
        validate=args.validate,
        limit=args.limit,
        savePlots=args.savePlots,
        weightType=args.weightType,
        fitRMSF=args.fitRMSF,
        phiMax_radm2=args.phiMax_radm2,
        dPhi_radm2=args.dPhi_radm2,
        nSamples=args.nSamples,
        polyOrd=args.polyOrd,
        noStokesI=args.noStokesI,
        showPlots=args.showPlots,
        not_RMSF=args.not_RMSF,
        rm_verbose=args.rm_verbose,
        debug=args.debug,
        fit_function=args.fit_function,
        tt0=args.tt0,
        tt1=args.tt1,
        ion=True,
        do_own_fit=args.do_own_fit,
        wait_for=[previous_future],
    ) if not args.skip_rmsynth else previous_future
    
    previous_future = rmclean_task.submit(
        field=args.field,
        outdir=args.datadir,
        host=host,
        username=args.username,
        password=args.password,
        dimension=args.dimension,
        verbose=args.verbose,
        database=args.database,
        validate=args.validate,
        limit=args.limit,
        cutoff=args.cutoff,
        maxIter=args.maxIter,
        gain=args.gain,
        window=args.window,
        showPlots=args.showPlots,
        rm_verbose=args.rm_verbose,
        wait_for=[previous_future],
    ) if not args.skip_rmclean else previous_future 
    
    previous_future = cat_task.submit(
        field=args.field,
        host=host,
        username=args.username,
        password=args.password,
        verbose=args.verbose,
        outfile=args.outfile,
        wait_for=[previous_future],
    ) if not args.skip_cat else previous_future

def save_args(args: configargparse.Namespace) -> Path:
    """Helper function to create a record of the input configuration arguments that
    govern the pipeline instance

    Args:
        args (configargparse.Namespace): Supplied arguments for the Arrakis pipeline instance

    Returns:
        Path: Output path of the saved file
    """
    args_yaml = yaml.dump(vars(args))
    args_yaml_f = os.path.abspath(f"{args.field}-config-{Time.now().fits}.yaml")
    logger.info(f"Saving config to '{args_yaml_f}'")
    with open(args_yaml_f, "w") as f:
        f.write(args_yaml)
    
    return Path(args_yaml_f)

def create_client(
    host: str, 
    dask_config: str, 
    field: str,
    use_mpi: bool,
    username: str,
    password: str,
    port_forward: Any
) -> Client: 
    
    if dask_config is None:
        config_dir = pkg_resources.resource_filename("arrakis", "configs")
        dask_config = f"{config_dir}/default.yaml"

    # Following https://github.com/dask/dask-jobqueue/issues/499
    with open(dask_config) as f:
        config = yaml.safe_load(f)

    config.update(
        {
            "log_directory": f"{field}_{Time.now().fits}_spice_logs/"
        }
    )
    if use_mpi:
        initialize(
            interface=config["interface"],
            local_directory=config["local_directory"],
            nthreads=config["cores"] / config["processes"],
        )
        client = Client()
    else:
        # TODO: load the cluster type and initialise it from field specified in the loaded config
        cluster = SLURMCluster(
            **config,
        )
        logger.debug(f"Submitted scripts will look like: \n {cluster.job_script()}")

        # cluster = LocalCluster(n_workers=10, processes=True, threads_per_worker=1, local_directory="/dev/shm",dashboard_address=f":{args.port}")
        client = Client(cluster)

    test_db(
        host=host,
        username=username,
        password=password,
    )

    port = client.scheduler_info()["services"]["dashboard"]

    # Forward ports
    if port_forward is not None:
        for p in port_forward:
            port_forward(port, p)

    # Prin out Dask client info
    logger.info(client.scheduler_info()["services"])

    return client

def create_dask_runner(*args, **kwargs) -> DaskTaskRunner:
    """Internally creates a Client object via `create_client`, 
    and then initialises a DaskTaskRunner. 

    Returns:
        DaskTaskRunner: A Prefect dask based task runner
    """
    client = create_client(*args, **kwargs)
    
    return DaskTaskRunner(client=client.schedular.address)

def main(args: configargparse.Namespace) -> None:
    """Main script

    Args:
        args (configargparse.Namespace): Command line arguments.
    """
    host = args.host
    
    # Lets save the args as a record for the ages
    output_args_path = save_args(args)
    logger.info(f"Saved arguments to {output_args_path}.")
    
    if args.outfile is None:
        outfile = f"{args.field}.pipe.test.fits"

    if args.skip_imager:
        # This is the client for the imager component of the arrakis 
        # pipeline. 
        dask_runner = create_dask_runner(
            host=args.host, 
            dask_config=args.imager_dask_config, 
            field=args.field,
            use_mpi=args.use_mpi,
            username=args.username,
            password=args.password,
            port_forward=args.port_forward
        )
        
        process_imager.with_options(
            name=f"Arrakis {args.field}",
            task_runner=dask_runner
        ).submit(args)
    else:
        logger.warn(f"Skipping the image creation step. ")
        
    # This is the client and pipeline for the RM extraction
    dask_runner = create_dask_runner(
        host=args.host, 
        dask_config=args.dask_config, 
        field=args.field,
        use_mpi=args.use_mpi,
        username=args.username,
        password=args.password,
        port_forward=args.port_forward
    )
    
    # Define flow
    process_spice.with_options(
        name=f"SPICE-RACS {args.field}",
        task_runner=dask_runner
    )(args, host)

    # TODO: Access the client via the `dask_runner`. Perhaps a 
    #       way to do this is to extend the DaskTaskRunner's 
    #       destructor and have it create it then. 
    # with performance_report(f"{args.field}-report-{Time.now().fits}.html"):
    #     executor = DaskExecutor(address=client.scheduler.address)
    #     flow.run(executor=executor)
    # client.close()


def cli():
    """Command-line interface"""
    # Help string to be shown using the -h option
    logostr = """
     mmm   mmm   mmm   mmm   mmm
     )-(   )-(   )-(   )-(   )-(
    ( S ) ( P ) ( I ) ( C ) ( E )
    |   | |   | |   | |   | |   |
    |___| |___| |___| |___| |___|
     mmm     mmm     mmm     mmm
     )-(     )-(     )-(     )-(
    ( R )   ( A )   ( C )   ( S )
    |   |   |   |   |   |   |   |
    |___|   |___|   |___|   |___|

    """

    descStr = f"""
    {logostr}
    Arrakis pipeline.

    Before running make sure to start a session of mongodb e.g.
        $ mongod --dbpath=/path/to/database --bind_ip $(hostname -i)

    """

    imager_parser = imager.imager_parser(parent_parser=True)

    # Parse the command line options
    parser = configargparse.ArgParser(
        default_config_files=[".default_config.txt"],
        description=descStr,
        formatter_class=configargparse.RawTextHelpFormatter,
        parents=[imager_parser]
    )
    parser.add("--config", required=False, is_config_file=True, help="Config file path")
    parser.add_argument(
        "field", metavar="field", type=str, help="Name of field (e.g. 2132-50A)."
    )

    parser.add_argument(
        "datadir",
        metavar="datadir",
        type=str,
        help="Directory containing data cubes in FITS format.",
    )

    parser.add_argument(
        "--host",
        default=None,
        type=str,
        help="Host of mongodb (probably $hostname -i).",
    )

    parser.add_argument(
        "--username", type=str, default=None, help="Username of mongodb."
    )

    parser.add_argument(
        "--password", type=str, default=None, help="Password of mongodb."
    )

    # parser.add_argument(
    #     '--port',
    #     type=int,
    #     default=9999,
    #     help="Port to run Dask dashboard on."
    # )
    parser.add_argument(
        "--use_mpi",
        action="store_true",
        help="Use Dask-mpi to parallelise -- must use srun/mpirun to assign resources.",
    )
    parser.add_argument(
        "--port_forward",
        default=None,
        help="Platform to fowards dask port [None].",
        nargs="+",
    )

    parser.add_argument(
        "--dask_config",
        type=str,
        default=None,
        help="Config file for Dask SlurmCLUSTER.",
    )
    parser.add_argument(
        "--imager_dask_config",
        type=str,
        default=None,
        help="Config file for Dask SlurmCLUSTER.",
    )
    parser.add_argument(
        "--holofile", type=str, default=None, help="Path to holography image"
    )

    parser.add_argument(
        "--yanda",
        type=str,
        default="1.3.0",
        help="Yandasoft version to pull from DockerHub [1.3.0].",
    )

    flowargs = parser.add_argument_group("pipeline flow options")
    flowargs.add_argument(
        "--skip_imaging", action="store_true", help="Skip imaging stage [False]."
    )
    flowargs.add_argument(
        "--skip_cutout", action="store_true", help="Skip cutout stage [False]."
    )
    flowargs.add_argument(
        "--skip_linmos", action="store_true", help="Skip LINMOS stage [False]."
    )
    flowargs.add_argument(
        "--skip_cleanup", action="store_true", help="Skip cleanup stage [False]."
    )
    flowargs.add_argument(
        "--skip_frion", action="store_true", help="Skip cleanup stage [False]."
    )
    flowargs.add_argument(
        "--skip_rmsynth", action="store_true", help="Skip RM Synthesis stage [False]."
    )
    flowargs.add_argument(
        "--skip_rmclean", action="store_true", help="Skip RM-CLEAN stage [False]."
    )
    flowargs.add_argument(
        "--skip_cat", action="store_true", help="Skip catalogue stage [False]."
    )

    options = parser.add_argument_group("output options")
    options.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output [False]."
    )
    options.add_argument(
        "-vw",
        "--verbose_worker",
        action="store_true",
        help="Verbose worker output [False].",
    )

    image_args = parser.add_argument_group("imaging arguments")
    image_args.add_argument("--psf_cutoff", type=float, help="Cutoff for smoothing")
    image_args.add_argument(
        "--robust",
        type=float,
        default=-0.5,
    )
    image_args.add_argument(
        "--nchan",
        type=int,
        default=36,
    )
    image_args.add_argument(
        "--pols",
        type=str,
        default="IQU",
    )
    image_args.add_argument(
        "--size",
        type=int,
        default=4096,
    )
    image_args.add_argument(
        "--scale",
        type=u.Quantity,
        default=2.5,
    )
    image_args.add_argument(
        "--mgain",
        type=float,
        default=0.8,
    )
    image_args.add_argument(
        "--niter",
        type=int,
        default=100_000,
    )
    image_args.add_argument(
        "--nmiter",
        type=int,
        default=None,
    )
    image_args.add_argument(
        "--auto_mask",
        type=float,
        default=3.0,
    )
    image_args.add_argument(
        "--auto-threshold",
        type=float,
        default=1.0,
    )
    image_args.add_argument(
        "--local-rms",
        action="store_true",
    )
    image_args.add_argument(
        "--force-mask-rounds",
        type=int,
        default=None,
    )
    image_args.add_argument(
        "--gridder",
        type=str,
        default=None,
        choices=["direct-ft", "idg", "wgridder", "tuned-wgridder", "wstacking"],
    )
    image_args.add_argument(
        "--taper",
        type=float,
        default=None,
    )
    image_args.add_argument(
        "--minuv",
        type=float,
        default=0.0,
    )
    image_args.add_argument(
        "--parallel",
        type=int,
        default=None,
    )
    image_args.add_argument(
        "--purge",
        action="store_true",
        help="Purge intermediate files",
    )
    image_args.add_argument(
        "--mpi",
        action="store_true",
        help="Use MPI",
    )
    image_args.add_argument(
        "--reimage",
        action="store_true",
        help="Force a new round of imaging. Otherwise, will skip if images already exist.",
    )

    cutargs = parser.add_argument_group("cutout arguments")
    cutargs.add_argument(
        "-p",
        "--pad",
        type=float,
        default=5,
        help="Number of beamwidths to pad around source [5].",
    )

    cutargs.add_argument("--dryrun", action="store_true", help="Do a dry-run [False].")

    synth = parser.add_argument_group("RM-synth/CLEAN arguments")

    synth.add_argument(
        "--dimension",
        default="1d",
        help="How many dimensions for RMsynth [1d] or '3d'.",
    )

    synth.add_argument(
        "-m",
        "--database",
        action="store_true",
        help="Add RMsynth data to MongoDB [False].",
    )

    synth.add_argument(
        "--tt0",
        default=None,
        type=str,
        help="TT0 MFS image -- will be used for model of Stokes I -- also needs --tt1.",
    )

    synth.add_argument(
        "--tt1",
        default=None,
        type=str,
        help="TT1 MFS image -- will be used for model of Stokes I -- also needs --tt0.",
    )

    synth.add_argument(
        "--validate", action="store_true", help="Run on RMsynth Stokes I [False]."
    )

    synth.add_argument(
        "--limit", default=None, type=int, help="Limit number of sources [All]."
    )
    synth.add_argument(
        "--own_fit",
        dest="do_own_fit",
        action="store_true",
        help="Use own Stokes I fit function [False].",
    )
    tools = parser.add_argument_group("RM-tools arguments")
    # RM-tools args
    tools.add_argument(
        "-sp", "--savePlots", action="store_true", help="save the plots [False]."
    )
    tools.add_argument(
        "-w",
        "--weightType",
        default="variance",
        help="weighting [variance] (all 1s) or 'uniform'.",
    )
    tools.add_argument(
        "--fit_function",
        type=str,
        default="log",
        help="Stokes I fitting function: 'linear' or ['log'] polynomials.",
    )
    tools.add_argument(
        "-t",
        "--fitRMSF",
        action="store_true",
        help="Fit a Gaussian to the RMSF [False]",
    )
    tools.add_argument(
        "-l",
        "--phiMax_radm2",
        type=float,
        default=None,
        help="Absolute max Faraday depth sampled (overrides NSAMPLES) [Auto].",
    )
    tools.add_argument(
        "-d",
        "--dPhi_radm2",
        type=float,
        default=None,
        help="Width of Faraday depth channel [Auto].",
    )
    tools.add_argument(
        "-s",
        "--nSamples",
        type=float,
        default=5,
        help="Number of samples across the FWHM RMSF.",
    )
    tools.add_argument(
        "-o",
        "--polyOrd",
        type=int,
        default=3,
        help="polynomial order to fit to I spectrum [3].",
    )
    tools.add_argument(
        "-i",
        "--noStokesI",
        action="store_true",
        help="ignore the Stokes I spectrum [False].",
    )
    tools.add_argument(
        "--showPlots", action="store_true", help="show the plots [False]."
    )
    tools.add_argument(
        "-R",
        "--not_RMSF",
        action="store_true",
        help="Skip calculation of RMSF? [False]",
    )
    tools.add_argument(
        "-rmv",
        "--rm_verbose",
        action="store_true",
        help="Verbose RMsynth/CLEAN [False].",
    )
    tools.add_argument(
        "-D",
        "--debug",
        action="store_true",
        help="turn on debugging messages & plots [False].",
    )
    # RM-tools args
    tools.add_argument(
        "-c",
        "--cutoff",
        type=float,
        default=-3,
        help="CLEAN cutoff (+ve = absolute, -ve = sigma) [-3].",
    )
    tools.add_argument(
        "-n",
        "--maxIter",
        type=int,
        default=10000,
        help="maximum number of CLEAN iterations [10000].",
    )
    tools.add_argument(
        "-g", "--gain", type=float, default=0.1, help="CLEAN loop gain [0.1]."
    )
    tools.add_argument(
        "--window",
        type=float,
        default=None,
        help="Further CLEAN in mask to this threshold [False].",
    )
    cat = parser.add_argument_group("catalogue arguments")
    # Cat args
    cat.add_argument(
        "--outfile", default=None, type=str, help="File to save table to [None]."
    )
    args = parser.parse_args()
    if not args.use_mpi:
        parser.print_values()

    verbose = args.verbose
    if verbose:
        logger.setLevel(logger.INFO)

    main(args)


if __name__ == "__main__":
    cli()