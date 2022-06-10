#!/usr/bin/env python3
"""Make a SPICE-RACS catalogue"""
import os
import time
import numpy as np
import warnings
from astropy.table import Table, Column
from astropy.io import fits
from astropy.io import votable as vot
from astropy.stats import sigma_clip, mad_std
import astropy.units as u
from tqdm import tqdm, trange
from spiceracs import columns_possum
from spiceracs.utils import get_db, test_db, get_field_db, latexify
from rmtable import RMTable
import logging as log
from pprint import pformat
from scipy.stats import lognorm, norm
import matplotlib.pyplot as plt
from typing import Optional, Union, Callable
from vorbin.voronoi_2d_binning import voronoi_2d_binning
import pandas as pd
from IPython import embed



def lognorm_from_percentiles(x1, p1, x2, p2):
    """ Return a log-normal distribuion X parametrized by:

            P(X < p1) = x1
            P(X < p2) = x2
    """
    x1 = np.log(x1)
    x2 = np.log(x2)
    p1ppf = norm.ppf(p1)
    p2ppf = norm.ppf(p2)

    scale = (x2 - x1) / (p2ppf - p1ppf)
    mean = ((x1 * p2ppf) - (x2 * p1ppf)) / (p2ppf - p1ppf)

    return scale, np.exp(mean)


def sigma_add_fix(tab):
    sigma_Q_low = np.array(tab['sigma_add_Q'] - tab['sigma_add_Q_err_minus'])
    sigma_Q_high = np.array(tab['sigma_add_Q'] + tab['sigma_add_Q_err_plus'])

    sigma_U_low = np.array(tab['sigma_add_U'] - tab['sigma_add_U_err_minus'])
    sigma_U_high = np.array(tab['sigma_add_U'] + tab['sigma_add_U_err_plus'])

    s_Q, scale_Q = lognorm_from_percentiles(
        sigma_Q_low,
        15.72/100,
        sigma_Q_high,
        84.27/100
    )

    s_U, scale_U = lognorm_from_percentiles(
        sigma_U_low,
        15.72/100,
        sigma_U_high,
        84.27/100
    )

    med, std = np.zeros_like(s_Q), np.zeros_like(s_Q)
    for i, (_s_Q, _scale_Q, _s_U, _scale_U) in tqdm(
        enumerate(zip(s_Q, scale_Q, s_U, scale_U)),
        total=len(s_Q),
        desc="Calculating sigma_add"
    ):
        try:
            Q_dist = lognorm.rvs(s=_s_Q, scale=_scale_Q, size=(1000))
            U_dist = lognorm.rvs(s=_s_U, scale=_scale_U, size=(1000))
            P_dist = np.hypot(Q_dist, U_dist)
            med[i] = np.median(P_dist)
            std[i] = np.std(P_dist)
        except ValueError:
            med[i] = np.nan
            std[i] = np.nan

    tab.add_column(Column(data=med, name='sigma_add'))
    tab.add_column(Column(data=std, name='sigma_add_err'))
    tab.remove_columns(
        [
            'sigma_add_Q',
            'sigma_add_U',
            'sigma_add_Q_err_minus',
            'sigma_add_Q_err_plus',
            'sigma_add_U_err_minus',
            'sigma_add_U_err_plus'
        ]
    )

    return tab


def is_leakage(frac, sep, fit):
    """Determine if a source is leakage

    Args:
        frac (float): Polarised fraction
        sep (float): Separation from tile centre
        fit (function): Fitting function

    Returns:
        bool: True if source is leakage
    """
    fit_frac = fit(sep)
    return frac < fit_frac


def get_fit_func(
    tab: Union[RMTable, Table], 
    nbins:int=21, 
    offset:float=0.002, 
    degree:int=2, 
    do_plot:bool=False
) -> Union[Callable, Optional[plt.Figure]]:
    """Fit an envelope to define leakage sources

    Args:
        tab (Union[RMTable, Table]): Catalogue to fit
        nbins (int, optional): Number of bins along seperation axis. Defaults to 21.
        offset (float, optional): Offset to fit envelope. Defaults to 0.002.
        degree (int, optional): Polynomial order of fit. Defaults to 2.
        do_plot (bool, optional): Make a plot of the leakage. Defaults to False.

    Returns:
        Union[np.polynomial.Polynomial.fit, Optional[plt.Figure]]: Polynomial 
        fit to the leakage and optional plot
    """    

    # Select high SNR sources
    hi_snr = (tab['stokesI'].to(u.Jy/u.beam) / tab['stokesI_err'].to(u.Jy/u.beam)) > 100
    hi_i_tab = tab[hi_snr]
    # Get fractional pol
    frac_P = np.array(hi_i_tab['fracpol'].value)
    # Bin sources by separation from tile centre
    bins = np.histogram_bin_edges(
        hi_i_tab['beamdist'].to(u.deg).value, bins=nbins)
    bins_c = np.median(np.vstack([bins[0:-1], bins[1:]]), axis=0)
    # Compute the median and standard deviation of the fractional pol
    meds = np.zeros_like(bins_c)
    s1_ups = np.zeros_like(bins_c)
    s1_los = np.zeros_like(bins_c)
    s2_ups = np.zeros_like(bins_c)
    s2_los = np.zeros_like(bins_c)
    for i in range(len(bins)-1):
        idx = ((hi_i_tab['beamdist'].to(u.deg).value < bins[i+1])
               & (hi_i_tab['beamdist'].to(u.deg).value >= bins[i]))
        s2_los[i], s1_los[i], meds[i], s1_ups[i], s2_ups[i] = np.nanpercentile(
            frac_P[idx], [2.3, 16, 50, 84, 97.6]
        )
    # Fit to median with small offset
    fit = np.polynomial.Polynomial.fit(
        bins_c, 
        meds+offset, 
        deg=degree, 
        full=False
    )
    if not do_plot: 
        return fit
    
    # Plot the fit
    latexify(columns=1)
    figure = plt.figure(facecolor='w')
    fig = plt.figure(facecolor='w')
    color = 'tab:green'
    stoke = {
        "s2_los": s2_los,
        "s1_los": s1_los,
        "meds": meds,
        "s1_ups": s1_ups,
        "s2_ups" :s2_ups,
    }
    plt.scatter(
        hi_i_tab['beamdist'].to(u.deg).value,
        frac_P, 
        s=1, 
        alpha=0.2,
        marker='.',
        c='k',
        zorder=0,
    )
    plt.plot(
        bins_c,
        meds,
        alpha=1,
        c=color,
        label="Median"
    )
    for s, ls in zip((1,2), ("--",":")):
        for r in ("ups", "los"):
            plt.plot(
                bins_c,
                stoke[f"s{s}_{r}"],
                alpha=1,
                c=color,
                linestyle=ls,
                label=f"${s}\sigma$" if r=="ups" else ""
            )
    xx = np.linspace(0, 4.5, 100)
    plt.plot(xx, fit(xx), 'tab:orange', label="Leakage envelope")
    plt.legend(loc='upper left')
    plt.xlabel('Separation from tile centre [deg]')
    plt.ylabel(f'$P/I$ fraction')
    plt.ylim(0,+0.05)
    plt.grid()
    return fit, fig


def cuts_and_flags(cat):
    """Cut out bad sources, and add flag columns

    A flag of 'True' means the source is bad.

    Args:
        cat (rmt): Catalogue to cut and flag
    """
    # SNR flag
    snr_flag = cat['snr_polint'] < 8
    cat.add_column(Column(data=snr_flag, name='snr_flag'))
    # Leakage flag
    fit = get_fit_func(cat)
    leakage_flag = is_leakage(
        cat['fracpol'].value,
        cat['beamdist'].to(u.deg).value,
        fit
    )
    cat.add_column(Column(data=leakage_flag, name='leakage_flag'))
    # Channel flag
    chan_flag = cat['Nchan'] < 144
    cat.add_column(Column(data=chan_flag, name='channel_flag'))
    # Fitting flag
    # 0: Improper input parameters (not sure what would trigger this in RM-Tools?) 
    # 1-4: One or more of the convergence criteria was met. 
    # 5: Reached maximum number of iterations before converging. 
    # 6-8: User defined limits for convergence are too small (should not occur, since RM-Tools uses default values) 
    # 9: fit failed, reason unknown
    # 16: a fit parameter has become infinite/numerical overflow
    # +64 (can be added to other flags): model gives Stokes I values with S:N < 1 for at least one channel
    # +128 (can be added to other flags): model gives Stokes I values < 0 for at least one channel
    fit_flag = cat['stokes_I_fit_flag'] > 5
    cat.remove_column('stokes_I_fit_flag')
    cat.add_column(Column(data=fit_flag, name='stokes_I_fit_flag'))
    # sigma_add flag
    sigma_flag = cat['sigma_add'] > 1
    cat.add_column(Column(data=sigma_flag, name='complex_sigma_add_flag'))
    # M2_CC flag
    m2_flag = cat['rm_width'] > cat['rmsf_fwhm']
    cat.add_column(Column(data=m2_flag, name='complex_M2_CC_flag'))

    # Flag RMs which are very diffent from RMs nearby
    # Set up voronoi bins, trying to obtain 50 sources per bin
    good_cat = cat[~snr_flag & ~leakage_flag & ~chan_flag & ~fit_flag]
    log.info("Computing voronoi bins and finding bad RMs")
    def sn_func(index, signal=None, noise=None):
        try:
            sn = len(np.array(index))  
        except TypeError:
            sn = 1
        return sn
    bin_number, x_gen, y_gen, x_bar, y_bar, sn, nPixels, scale = voronoi_2d_binning(
        x=good_cat['ra'], 
        y=good_cat['dec'], 
        signal=np.ones_like(good_cat['polint']),
        noise=np.ones_like(good_cat['polint_err']),
        target_sn=50, 
        sn_func=sn_func,
        cvt=False, 
        pixelsize=10, 
        plot=False,
        quiet=True, 
        wvt=False
    )
    log.info(f"Found {len(bin_number)} bins")
    df = good_cat.to_pandas()
    df.set_index("cat_id", inplace=True)
    df['bin_number'] = bin_number
    # Use sigma clipping to find outliers
    def masker(x):
        return pd.Series(sigma_clip(x['rm'], sigma=3, maxiters=None, cenfunc=np.median).mask, index=x.index)
    perc_g = df.groupby("bin_number").apply(
        masker,
    )
    # Put flag into the catalogue
    df["local_rm_flag"] = perc_g.reset_index().set_index("cat_id")[0]
    df.drop(columns=["bin_number"], inplace=True)
    df_out = cat.to_pandas()
    df_out.set_index("cat_id", inplace=True)
    df_out["local_rm_flag"] = [False] * len(df_out)
    df_out.update(df[["local_rm_flag"]])
    cat = RMTable.from_pandas(df_out)
    return cat, fit

def get_alpha(cat):
    coefs_str = cat["stokesI_model_coef"]
    coefs_err_str = cat["stokesI_model_coef_err"]
    alphas = []
    alphas_err = []
    for c, c_err in zip(coefs_str, coefs_err_str):
        coefs = c.split(",")
        coefs_err = c_err.split(",")
        alpha = float(coefs[-2]) # alpha is the 2nd last coefficient
        alpha_err = float(coefs_err[-2])
        alphas.append(alpha)
        alphas_err.append(alpha_err)
    return np.array(alphas), np.array(alphas_err)

def get_integration_time(cat, field_col):
    field_names = list(cat['tile_id'])
    query = {
        "$and": [
            {"FIELD_NAME": {"$in": field_names}},
            {"SELECT": 1}
        ]
    }
    tint_dicts = list(field_col.find(query, {"_id":0,"SCAN_TINT": 1, "FIELD_NAME": 1}))
    tint_dict = {}
    for d in tint_dicts:
        tint_dict.update(
                {
                    d["FIELD_NAME"]: d["SCAN_TINT"]
                }
        )

    tints = []
    for name in field_names:
        tints.append(tint_dict[name])
    
    return np.array(tints) * u.s

# Stolen from GASKAP pipeline
# Credit to J. Dempsey
# https://github.com/GASKAP/GASKAP-HI-Absorption-Pipeline/
# https://github.com/GASKAP/GASKAP-HI-Absorption-Pipeline/blob/
# def add_col_metadata(vo_table, col_name, description, units=None, ucd=None, datatype=None):
#     """Add metadata to a VO table column.

#     Args:
#         vo_table (vot.): VO Table
#         col_name (str): Column name
#         description (str): Long description of the column
#         units (u.Unit, optional): Unit of column. Defaults to None.
#         ucd (str, optional): UCD string. Defaults to None.
#         datatype (_type_, optional): _description_. Defaults to None.
#     """    
#     col = vo_table.get_first_table().get_field_by_id(col_name)
#     col.description = description
#     if units:
#         col.unit = units
#     if ucd:
#         col.ucd = ucd
#     if datatype:
#         col.datatype = datatype

def add_metadata(vo_table: vot.tree.Table, filename: str):
    """Add metadata to VO Table for CASDA

    Args:
        vo_table (vot): VO Table object

    Returns:
        vot: VO Table object with metadata
    """    
    # Add extra metadata
    for col_name, meta in columns_possum.extra_column_descriptions.items():
        col = vo_table.get_first_table().get_field_by_id(col_name)
        col.description = meta["description"]
        col.ucd = meta["ucd"]

    # Add params for CASDA
    if len(vo_table.params) > 0:
        log.warning(f"{filename} already has params - not adding")
        return vo_table
    _ , ext = os.path.splitext(filename)
    cat_name = os.path.basename(filename).replace(ext, "").replace(".","_").replace("-","_")
    idx_fields = "ra,dec,cat_id,source_id"
    pri_fields = "ra,dec,cat_id,source_id,rm,polint,snr_polint,fracpol,stokesI,sigma_add"
    params = [
        vot.tree.Param(
            vo_table,
            ID="Catalogue_Name", 
            name="Catalogue Name", 
            value=cat_name,
            arraysize=str(len(cat_name))
        ),
        vot.tree.Param(
            vo_table,
            ID="Indexed_Fields", 
            name="Indexed Fields", 
            value=idx_fields,
            arraysize=str(len(idx_fields))
        ),
        vot.tree.Param(
            vo_table,
            ID="Principal_Fields", 
            name="Principal Fields", 
            value=pri_fields,
            arraysize=str(len(pri_fields))
        ),
    ]
    vo_table.get_first_table().params.extend(params)

    return vo_table

def replace_nans(filename:str):
    """Replace NaNs in a XML table with a string

    Args:
        filename (str): File name
    """  
    pass
    # with open(filename, "r") as f:
    #     xml = f.read()
    # xml = xml.replace("NaN", "null")
    # with open(filename, "w") as f:
    #     f.write(xml)

def write_votable(rmtab:RMTable,outfile:str) -> None:
    # CASDA needs v1.3
    vo_table = vot.from_table(rmtab)
    vo_table.version = "1.3"
    vo_table = add_metadata(vo_table, outfile)
    vot.writeto(vo_table, outfile)
    # Fix NaNs for CASDA
    replace_nans(outfile)

def main(
    field: str,
    host: str,
    username: str = None,
    password: str = None,
    verbose=True,
    outfile: str = None,
) -> None:
    """Main

    Args:
        field (str): RACS field name
        host (str): MongoDB host IP
        username (str, optional): Mongo username. Defaults to None.
        password (str, optional): Mongo password. Defaults to None.
        verbose (bool, optional): Verbose output. Defaults to True.
        outfile (str, optional): Output file name. Defaults to None.
        cat_format (str, optional): Type of catalogue .e.g. fits. Defaults to None.
    """
    # default connection (ie, local)
    beams_col, island_col, comp_col = get_db(
        host=host, username=username, password=password
    )
    log.info("Starting beams collection query")
    tick = time.time()
    query = {
        "$and": [{f"beams.{field}": {"$exists": True}}, {f"beams.{field}.DR1": True}]
    }
    all_island_ids = sorted(beams_col.distinct("Source_ID", query))
    tock = time.time()
    log.info(f"Finished beams collection query - {tock-tick:.2f}s")

    log.info("Starting component collection query")
    tick = time.time()
    query = {
        "$and": [
            {"Source_ID": {"$in": all_island_ids}}, 
            {"rmsynth1d": True},
            {"rmclean1d": True},
        ]
    }

    fields = {}
    for n in columns_possum.input_names:
        fields.update({n: 1})
    for n in columns_possum.sourcefinder_columns:
        fields.update({n: 1})
    fields.update({"rmsynth_summary": 1})
    fields.update({"rmclean_summary": 1})
    fields.update({"header": 1})

    comps = list(
        comp_col.find(
            query,
            fields
        )
    )
    tock = time.time()
    log.info(f"Finished component collection query - {tock-tick:.2f}s")

    rmtab = RMTable() # type: RMTable
    # Add items to main cat using RMtable standard
    for j, [name, typ, src, col, unit] in enumerate(
        tqdm(
            zip(
                columns_possum.output_cols,
                columns_possum.output_types,
                columns_possum.input_sources,
                columns_possum.input_names,
                columns_possum.output_units,
            ),
            total=len(columns_possum.output_cols),
            desc="Making table by column",
            disable=not verbose,
        ),
    ):
        data = []
        if src == "cat":
            for comp in comps:
                data += [comp[col]]
            new_col = Column(data=data, name=name, dtype=typ, unit=unit)
            rmtab.add_column(new_col)

        if src == "synth":
            for comp in comps:
                try:
                    data += [comp["rmclean_summary"][col]]
                except KeyError:
                    data += [comp["rmsynth_summary"][col]]
            new_col = Column(data=data, name=name, dtype=typ, unit=unit)
            rmtab.add_column(new_col)

        if src == "header":
            for comp in comps:
                data += [comp["header"][col]]
            new_col = Column(data=data, name=name, dtype=typ, unit=unit)
            rmtab.add_column(new_col)

    for selcol in tqdm(columns_possum.sourcefinder_columns, desc="Adding BDSF data"):
        data = []
        for comp in comps:
            data += [comp[selcol]]
        new_col = Column(data=data, name=selcol)
        rmtab.add_column(new_col)
    
    # Fix sigma_add
    rmtab = sigma_add_fix(rmtab)

    # Add flags
    rmtab, fit = cuts_and_flags(rmtab)

    # Add spectral index from fitted model
    alphas, alphas_err = get_alpha(rmtab)
    rmtab.add_column(Column(data=alphas, name='spectral_index'))
    rmtab.add_column(Column(data=alphas_err, name='spectral_index_err'))

    # Add integration time
    field_col = get_field_db(
        host=host, username=username, password=password
    )
    tints = get_integration_time(rmtab, field_col)
    rmtab.add_column(Column(data=tints, name='int_time'))
    # Add epoch
    rmtab.add_column(Column(data=rmtab['start_time'] + (tints / 2), name='epoch'))

    # Get Galatic coords
    glon, glat = RMTable.calculate_missing_coordinates_column(
        rmtab["ra"].to(u.deg), rmtab["dec"].to(u.deg), to_galactic=True
    )
    rmtab.add_column(col=glon*u.deg, name="l")
    rmtab.add_column(col=glat*u.deg, name="b")
    rmtab.add_column(
        col=np.max(
            [
                rmtab['ra_err'].to(u.arcsec), 
                rmtab['dec_err'].to(u.arcsec)
            ]
        ) * u.arcsec, 
        name="pos_err"
    )

    # Add common columns
    rmtab["rm_method"] = "RM Synthesis - Fractional polarization"
    rmtab["telescope"] = "ASKAP"
    rmtab["pol_bias"] = "2012PASA...29..214G"
    rmtab["catalog"] = "SPICE-RACS-DR1"
    rmtab["ionosphere"] = "FRion"
    rmtab["flux_type"] = "Peak"
    rmtab["aperture"] = 0*u.deg

    rmtab.add_column(
        col=fit(
            rmtab["separation_tile_centre"].to(u.deg).value,
        ),
        name="leakage"
    )

    rmtab.add_column(
        col=np.logical_or(
            rmtab['complex_sigma_add_flag'], 
            rmtab['complex_M2_CC_flag']
        ), 
        name='complex_flag'
    )

    # Verify table
    rmtab.add_missing_columns()
    rmtab.verify_standard_strings()
    rmtab.verify_limits()
    # Readd complex test
    rmtab["complex_test"]  = "sigma_add OR Second moment"
    # Add main ID
    rmtab['cat_id'].meta['ucd'] = 'meta.id;meta.main'
    rmtab.ucds['cat_id'] = 'meta.id;meta.main'
    rmtab["cat_id"].description = "Gaussian ID"
    # Check ucds
    for colname, ucd in rmtab.ucds.items():
        check = vot.ucd.check_ucd(ucd)
        if not check:
            log.warning(f"{colname} has invalid ucd '{ucd}'")

    if outfile is None:
        log.info(pformat(rmtab))

    if outfile is not None:
        _ , ext = os.path.splitext(outfile)
        if ext == ".xml" or ext == ".vot":
            write_votable(rmtab, outfile)
        else:
            rmtab.write(outfile, overwrite=True)
        log.info(f"{outfile} written to disk")

    log.info("Done!")


def cli():
    """Command-line interface"""
    import argparse
    from astropy.utils.exceptions import AstropyWarning

    warnings.simplefilter("ignore", category=AstropyWarning)
    from astropy.io.fits.verify import VerifyWarning

    warnings.simplefilter("ignore", category=VerifyWarning)
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

    # Help string to be shown using the -h option
    descStr = f"""
    {logostr}
    SPICE-RACS Stage 7:
    Make RM catalogue.

    """

    # Parse the command line options
    parser = argparse.ArgumentParser(
        description=descStr, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "field", metavar="field", type=str, help="RACS field to mosaic - e.g. 2132-50A."
    )

    parser.add_argument(
        "host",
        metavar="host",
        type=str,
        help="Host of mongodb (probably $hostname -i).",
    )

    parser.add_argument(
        "--username", type=str, default=None, help="Username of mongodb."
    )

    parser.add_argument(
        "--password", type=str, default=None, help="Password of mongodb."
    )

    parser.add_argument(
        "-v", "--verbose", action="store_true", help="verbose output [False]."
    )

    parser.add_argument(
        "-w",
        "--write",
        dest="outfile",
        default=None,
        type=str,
        help="File to save table to [None].",
    )

    args = parser.parse_args()

    verbose = args.verbose

    if verbose:
        log.basicConfig(
            level=log.INFO,
            format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True
        )
    else:
        log.basicConfig(
            format="%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True
        )

    host = args.host
    test_db(
        host=args.host, username=args.username, password=args.password, verbose=verbose
    )

    main(
        field=args.field,
        host=host,
        username=args.username,
        password=args.password,
        verbose=verbose,
        outfile=args.outfile,
    )


if __name__ == "__main__":
    cli()
