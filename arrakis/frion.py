#!/usr/bin/env python3
"""Correct for the ionosphere in parallel"""
import argparse
import logging
import os
from glob import glob
from pathlib import Path
from pprint import pformat
from shutil import copyfile
from typing import Callable, Dict, List
from typing import NamedTuple as Struct
from typing import Optional, Union

import astropy.units as u
import numpy as np
import pymongo
from astropy.time import Time, TimeDelta
from FRion import correct, predict
from prefect import flow, task, unmapped

from arrakis.logger import UltimateHelpFormatter, logger
from arrakis.utils.database import (
    get_db,
    get_field_db,
    test_db,
    validate_sbid_field_pair,
)
from arrakis.utils.fitsutils import getfreq
from arrakis.utils.io import try_mkdir
from arrakis.utils.pipeline import generic_parser, logo_str

logger.setLevel(logging.INFO)


class Prediction(Struct):
    """FRion prediction"""

    predict_file: str
    update: pymongo.UpdateOne


@task(name="FRion correction")
def correct_worker(
    beam: Dict, outdir: str, field: str, prediction: Prediction, island: dict
) -> pymongo.UpdateOne:
    """Apply FRion corrections to a single island

    Args:
        beam (Dict): MongoDB beam document
        outdir (str): Output directory
        field (str): RACS field name
        predict_file (str): FRion prediction file
        island_id (str): RACS island ID

    Returns:
        pymongo.UpdateOne: Pymongo update query
    """
    predict_file = prediction.predict_file
    island_id = island["Source_ID"]
    qfile = os.path.join(outdir, beam["beams"][field]["q_file"])
    ufile = os.path.join(outdir, beam["beams"][field]["u_file"])

    qout = beam["beams"][field]["q_file"].replace(".fits", ".ion.fits")
    uout = beam["beams"][field]["u_file"].replace(".fits", ".ion.fits")

    qout_f = os.path.join(outdir, qout)
    uout_f = os.path.join(outdir, uout)

    correct.apply_correction_to_files(
        qfile, ufile, predict_file, qout_f, uout_f, overwrite=True
    )

    myquery = {"Source_ID": island_id}

    newvalues = {
        "$set": {
            f"beams.{field}.q_file_ion": qout,
            f"beams.{field}.u_file_ion": uout,
        }
    }
    return pymongo.UpdateOne(myquery, newvalues)


@task(name="FRion predction")
def predict_worker(
    island: Dict,
    field: str,
    beam: Dict,
    start_time: Time,
    end_time: Time,
    freq: np.ndarray,
    cutdir: str,
    plotdir: str,
    server: str = "ftp://ftp.aiub.unibe.ch/CODE/",
    prefix: str = "",
    formatter: Optional[Union[str, Callable]] = None,
    proxy_server: Optional[str] = None,
    pre_download: bool = False,
) -> Prediction:
    """Make FRion prediction for a single island

    Args:
        island (Dict): Pymongo island document
        field (str): RACS field name
        beam (Dict): Pymongo beam document
        start_time (Time): Start time of the observation
        end_time (Time): End time of the observation
        freq (np.ndarray): Array of frequencies with units
        cutdir (str): Cutout directory
        plotdir (str): Plot directory

    Returns:
        Tuple[str, pymongo.UpdateOne]: FRion prediction file and pymongo update query
    """
    logger.setLevel(logging.INFO)

    ifile = os.path.join(cutdir, beam["beams"][field]["i_file"])
    i_dir = os.path.dirname(ifile)
    iname = island["Source_ID"]
    ra = island["RA"]
    dec = island["Dec"]

    # Tricking the ionex lookup to use the a custom server
    proxy_args = {
        "proxy_type": None,
        "proxy_port": None,
        "proxy_user": None,
        "proxy_pass": None,
    }
    logger.info("Set up empty Proxy structure.")

    times, RMs, theta = predict.calculate_modulation(
        start_time=start_time.fits,
        end_time=end_time.fits,
        freq_array=freq,
        telescope_location=predict.get_telescope_coordinates("ASKAP"),
        ra=ra,
        dec=dec,
        timestep=300.0,
        ionexPath=os.path.join(os.path.dirname(cutdir), "IONEXdata"),
        server=server,
        proxy_server=proxy_server,
        use_proxy=True,  # Always use proxy - forces urllib
        prefix=prefix,
        formatter=formatter,
        pre_download=pre_download,
        **proxy_args,
    )
    predict_file = os.path.join(i_dir, f"{iname}_ion.txt")
    predict.write_modulation(freq_array=freq, theta=theta, filename=predict_file)

    plot_file = os.path.join(i_dir, f"{iname}_ion.png")
    try:
        predict.generate_plots(
            times, RMs, theta, freq, position=[ra, dec], savename=plot_file
        )
    except Exception as e:
        logger.error(f"Failed to generate plot: {e}")

    plot_files = glob(os.path.join(i_dir, "*ion.png"))
    logger.info(f"Plotting files: {plot_files=}")
    for src in plot_files:
        base = os.path.basename(src)
        dst = os.path.join(plotdir, base)
        copyfile(src, dst)

    myquery = {"Source_ID": iname}

    newvalues = {
        "$set": {
            "frion": {
                "times": (
                    times.mjd * 86400.0
                ).tolist(),  # Turn back into MJD seconds for backwards compatibility
                "RMs": RMs.tolist(),
                "theta_real": theta.real.tolist(),
                "theta_imag": theta.imag.tolist(),
            }
        }
    }

    update = pymongo.UpdateOne(myquery, newvalues)

    return Prediction(predict_file, update)


@task(name="Index beams")
def index_beams(island: dict, beams: List[dict]) -> dict:
    island_id = island["Source_ID"]
    beam_idx = [i for i, b in enumerate(beams) if b["Source_ID"] == island_id][0]
    beam = beams[beam_idx]
    return beam


@flow(name="FRion")
def main(
    field: str,
    outdir: Path,
    host: str,
    epoch: int,
    sbid: Optional[int] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    database=False,
    ionex_server: str = "ftp://ftp.aiub.unibe.ch/CODE/",
    ionex_prefix: str = "codg",
    ionex_proxy_server: Optional[str] = None,
    ionex_formatter: Optional[Union[str, Callable]] = "ftp.aiub.unibe.ch",
    ionex_predownload: bool = False,
    limit: Optional[int] = None,
):
    """FRion flow

    Args:
        field (str): RACS field name
        outdir (Path): Output directory
        host (str): MongoDB host IP address
        epoch (int): Epoch of observation
        sbid (int, optional): SBID of observation. Defaults to None.
        username (str, optional): Mongo username. Defaults to None.
        password (str, optional): Mongo passwrod. Defaults to None.
        database (bool, optional): Update database. Defaults to False.
        ionex_server (str, optional): IONEX server. Defaults to "ftp://ftp.aiub.unibe.ch/CODE/".
        ionex_proxy_server (str, optional): Proxy server. Defaults to None.
        ionex_formatter (Union[str, Callable], optional): IONEX formatter. Defaults to "ftp.aiub.unibe.ch".
        ionex_predownload (bool, optional): Pre-download IONEX files. Defaults to False.
        limit (int, optional): Limit to number of islands. Defaults to None.
    """
    # Query database for data
    outdir = outdir.absolute()
    cutdir = outdir / "cutouts"

    plotdir = cutdir / "plots"
    plotdir.mkdir(parents=True, exist_ok=True)

    beams_col, island_col, comp_col = get_db(
        host=host, epoch=epoch, username=username, password=password
    )
    # Check for SBID match
    if sbid is not None:
        field_col = get_field_db(
            host=host,
            epoch=epoch,
            username=username,
            password=password,
        )
        sbid_check = validate_sbid_field_pair(
            field_name=field,
            sbid=sbid,
            field_col=field_col,
        )
        if not sbid_check:
            raise ValueError(f"SBID {sbid} does not match field {field}")

    query_1 = {"$and": [{f"beams.{field}": {"$exists": True}}]}

    if sbid is not None:
        query_1["$and"].append({f"beams.{field}.SBIDs": sbid})

    beams = list(beams_col.find(query_1).sort("Source_ID"))
    island_ids = sorted(beams_col.distinct("Source_ID", query_1))

    # Get FRion arguments
    query_2 = {"Source_ID": {"$in": island_ids}}
    islands = list(island_col.find(query_2).sort("Source_ID"))

    field_col = get_field_db(
        host=host, epoch=epoch, username=username, password=password
    )
    # SELECT '1' is best field according to the database
    query_3 = {"$and": [{"FIELD_NAME": f"{field}"}, {"SELECT": 1}]}
    if sbid is not None:
        query_3["$and"].append({"SBID": sbid})
    logger.info(f"{query_3}")

    # Raise error if too much or too little data
    if field_col.count_documents(query_3) > 1:
        logger.error(f"More than one SELECT=1 for {field} - try supplying SBID.")
        raise ValueError(f"More than one SELECT=1 for {field} - try supplying SBID.")

    elif field_col.count_documents(query_3) == 0:
        logger.error(f"No data for {field} with {query_3}, trying without SELECT=1.")
        query_3 = query_3 = {"$and": [{"FIELD_NAME": f"{field}"}]}
        if sbid is not None:
            query_3["$and"].append({"SBID": sbid})
        field_data = field_col.find_one({"FIELD_NAME": f"{field}"})
    else:
        logger.info(f"Using {query_3}")
        field_data = field_col.find_one({"FIELD_NAME": f"{field}"})

    logger.info(f"{field_data=}")

    start_time = Time(field_data["SCAN_START"] * u.second, format="mjd")
    end_time = start_time + TimeDelta(field_data["SCAN_TINT"] * u.second)

    freq = getfreq(
        os.path.join(cutdir, f"{beams[0]['beams'][f'{field}']['q_file']}"),
    )

    if limit is not None:
        logger.info(f"Limiting to {limit} islands")
        islands = islands[:limit]

    beams_cor = []
    for island in islands:
        island_id = island["Source_ID"]
        beam_idx = [i for i, b in enumerate(beams) if b["Source_ID"] == island_id][0]
        beam = beams[beam_idx]
        beams_cor.append(beam)

    # do one prediction to get the IONEX files
    _ = predict_worker(
        island=islands[0],
        field=field,
        beam=beams_cor[0],
        start_time=start_time,
        end_time=end_time,
        freq=freq.to(u.Hz).value,
        cutdir=cutdir,
        plotdir=plotdir,
        server=ionex_server,
        prefix=ionex_prefix,
        proxy_server=ionex_proxy_server,
        formatter=ionex_formatter,
        pre_download=ionex_predownload,
    )
    predictions = predict_worker.map(
        island=islands,
        field=unmapped(field),
        beam=beams_cor,
        start_time=unmapped(start_time),
        end_time=unmapped(end_time),
        freq=unmapped(freq.to(u.Hz).value),
        cutdir=unmapped(cutdir),
        plotdir=unmapped(plotdir),
        server=unmapped(ionex_server),
        prefix=unmapped(ionex_prefix),
        proxy_server=unmapped(ionex_proxy_server),
        formatter=unmapped(ionex_formatter),
        pre_download=unmapped(ionex_predownload),
    )

    corrections = correct_worker.map(
        beam=beams_cor,
        outdir=unmapped(cutdir),
        field=unmapped(field),
        prediction=predictions,
        island=islands,
    )

    updates_arrays = [p.result().update for p in predictions]
    updates = [c.result() for c in corrections]
    if database:
        logger.info("Updating beams database...")
        db_res = beams_col.bulk_write(updates, ordered=False)
        logger.info(pformat(db_res.bulk_api_result))

        logger.info("Updating island database...")
        db_res = island_col.bulk_write(updates_arrays, ordered=False)
        logger.info(pformat(db_res.bulk_api_result))


def frion_parser(parent_parser: bool = False) -> argparse.ArgumentParser:
    # Help string to be shown using the -h option
    descStr = f"""
    {logo_str}
    Arrakis Stage:
    Correct for ionospheric Faraday rotation

    """

    # Parse the command line options
    frion_parser = argparse.ArgumentParser(
        add_help=not parent_parser,
        description=descStr,
        formatter_class=UltimateHelpFormatter,
    )
    parser = frion_parser.add_argument_group("frion arguments")

    parser.add_argument(
        "--ionex_server",
        type=str,
        default="ftp://ftp.aiub.unibe.ch/CODE/",
        help="IONEX server",
    )

    parser.add_argument(
        "--ionex_prefix",
        type=str,
        default="codg",
    )

    parser.add_argument(
        "--ionex_formatter",
        type=str,
        default="ftp.aiub.unibe.ch",
        help="IONEX formatter.",
    )

    parser.add_argument(
        "--ionex_proxy_server",
        type=str,
        default=None,
        help="Proxy server.",
    )

    parser.add_argument(
        "--ionex_predownload",
        action="store_true",
        help="Pre-download IONEX files.",
    )

    return frion_parser


def cli():
    """Command-line interface"""
    import warnings

    from astropy.utils.exceptions import AstropyWarning

    warnings.simplefilter("ignore", category=AstropyWarning)
    from astropy.io.fits.verify import VerifyWarning

    warnings.simplefilter("ignore", category=VerifyWarning)
    warnings.simplefilter("ignore", category=RuntimeWarning)

    gen_parser = generic_parser(parent_parser=True)
    f_parser = frion_parser(parent_parser=True)
    parser = argparse.ArgumentParser(
        parents=[gen_parser, f_parser],
        formatter_class=UltimateHelpFormatter,
        description=f_parser.description,
    )
    args = parser.parse_args()

    verbose = args.verbose
    if verbose:
        logger.setLevel(logging.INFO)

    test_db(host=args.host, username=args.username, password=args.password)

    main(
        field=args.field,
        sbid=args.sbid,
        outdir=Path(
            args.datadir,
        ),
        host=args.host,
        epoch=args.epoch,
        username=args.username,
        password=args.password,
        database=args.database,
        ionex_server=args.ionex_server,
        ionex_proxy_server=args.ionex_proxy_server,
        ionex_formatter=args.ionex_formatter,
        ionex_prefix=args.ionex_prefix,
        ionex_predownload=args.ionex_predownload,
    )


if __name__ == "__main__":
    cli()
