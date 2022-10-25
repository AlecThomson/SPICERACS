#!/bin/bash -l

#SBATCH --job-name=SPICE-CASDA
#SBATCH --export=NONE
#SBATCH --mail-user=alec.thomson@csiro.au
#SBATCH --mail-type=ALL
#SBATCH -e /group/askap/athomson/projects/spiceracs/spica/slurmLogs/casda_prep_slurm-%j.log
#SBATCH -o /group/askap/athomson/projects/spiceracs/spica/slurmLogs/casda_prep_slurm-%j.log

#SBATCH --cluster=zeus
#SBATCH --account=askap
#SBATCH --ntasks=48
#SBATCH --time=1-00:00:00
#SBATCH --partition=highmemq
#SBATCH --mem=1000GB

# conda activate spice
module load intel-mpi
conda activate spice-zeus

data_dir=/group/askap/athomson/projects/spiceracs/DR1/full_spica
polcat=/group/askap/athomson/projects/spiceracs/DR1/spice-racs.dr1.corrected.xml

cd $data_dir

srun -n $SLURM_NTASKS casda_prepare.py $data_dir $polcat --convert-spectra -v --mpi --batch_size 10_000 --interface ib0 --outdir /scratch/ja3/athomson/spica
srun -n $SLURM_NTASKS casda_prepare.py $data_dir $polcat --update-cubes --convert-plots -v --mpi --batch_size 10_000 --interface ib0 --outdir /scratch/ja3/athomson/spica