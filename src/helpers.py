import os
import sys
import subprocess
import gzip
import re

from typing import Dict, Optional
from Bio import SeqIO #type: ignore


from src.utils import find_closest_in_list

from src.utilities.cupcake.sequence.err_correct_w_genome import err_correct
from src.utilities.cupcake.sequence.sam_to_gff3 import convert_sam_to_gff3

from src.config import seqid_rex1, seqid_rex2, seqid_fusion
from src.commands import get_aligner_command, GFFREAD_PROG, run_command, run_gmst
from src.parsers import parse_GMST, parse_corrORF

### Environment manipulation functions ###
def rename_isoform_seqids(input_fasta, force_id_ignore=False):
    """
    Rename input isoform fasta/fastq, which is usually mapped, collapsed Iso-Seq data with IDs like:

    PB.1.1|chr1:10-100|xxxxxx

    to just being "PB.1.1"

    :param input_fasta: Could be either fasta or fastq, autodetect. Can be gzipped.
    :return: output fasta with the cleaned up sequence ID, is_fusion flag
    """
    type = 'fasta'
    # gzip.open and open have different default open modes:
    # gzip.open uses "rb" (read in binary format)
    # open uses "rt" (read in text format)
    # This can be solved by making explicit the read text mode (which is required
    # by SeqIO.parse)
    if input_fasta.endswith('.gz'):
        open_function = gzip.open
        in_file = os.path.splitext(input_fasta)[0]
        out_file = os.path.splitext(in_file)[0] + '.renamed.fasta'
    else:
        open_function = open
        out_file = os.path.splitext(input_fasta)[0] + '.renamed.fasta'
    with open_function(input_fasta, mode="rt") as h:
        if h.readline().startswith('@'): type = 'fastq'
    f = open(out_file, mode='wt')
    for r in SeqIO.parse(open_function(input_fasta, "rt"), type):
        m1 = seqid_rex1.match(r.id)
        m2 = seqid_rex2.match(r.id)
        m3 = seqid_fusion.match(r.id)
        if not force_id_ignore and (m1 is None and m2 is None and m3 is None):
            print("Invalid input IDs! Expected PB.X.Y or PB.X.Y|xxxxx or PBfusion.X format but saw {0} instead. Abort!".format(r.id), file=sys.stderr)
            sys.exit(1)
        if r.id.startswith('PB.') or r.id.startswith('PBfusion.'):  # PacBio fasta header
            newid = r.id.split('|')[0]
        else:
            raw = r.id.split('|')
            if len(raw) > 4:  # RefSeq fasta header
                newid = raw[3]
            else:
                newid = r.id.split()[0]  # Ensembl fasta header
        f.write(">{0}\n{1}\n".format(newid, r.seq))
    f.close()
    return out_file


### Input/Output functions ###

def get_corr_filenames(outdir, prefix):
    corrPathPrefix = os.path.abspath(os.path.join(outdir, prefix))
    corrGTF = corrPathPrefix + "_corrected.gtf"
    corrSAM = corrPathPrefix + "_corrected.sam"
    corrFASTA = corrPathPrefix + "_corrected.fasta"
    corrORF = corrPathPrefix + "_corrected.faa"
    corrCDS_GTF_GFF = corrPathPrefix + "_corrected.gtf.cds.gff"
    return corrGTF, corrSAM, corrFASTA, corrORF, corrCDS_GTF_GFF

def get_isoform_hits_name(outdir, prefix):
    corrPathPrefix = os.path.abspath(os.path.join(outdir, prefix))
    isoform_hits_name = corrPathPrefix + "_isoform_hits.txt"
    return isoform_hits_name

def get_class_junc_filenames(outdir, prefix):
    outputPathPrefix = os.path.abspath(os.path.join(outdir, prefix))
    outputClassPath = outputPathPrefix + "_classification.txt"
    outputJuncPath = outputPathPrefix + "_junctions.txt"
    return outputClassPath, outputJuncPath

def get_pickle_filename(outdir, prefix):
    pklPathPrefix = os.path.abspath(os.path.join(outdir, prefix))
    pklFilePath = pklPathPrefix + ".isoforms_info.pkl"
    return pklFilePath

def get_omitted_name(outdir, prefix):
    corrPathPrefix = os.path.abspath(os.path.join(outdir, prefix))
    omitted_name = corrPathPrefix + "_omitted_due_to_min_ref_len.txt"
    return omitted_name

def sequence_correction(
    outdir: str,
    output: str,
    cpus: int,
    chunks: int,
    fasta: bool,
    genome_dict: Dict[str, str],
    badstrandGTF: str,
    genome: str,
    isoforms: str,
    aligner_choice: str,
    gmap_index: Optional[str] = None,
    annotation: Optional[str] = None
    ) -> None:
    """
    Use the reference genome to correct the sequences (unless a pre-corrected GTF is given)
    """
    print("Correcting sequences")
    corrGTF, corrSAM, corrFASTA, _ , _ = get_corr_filenames(outdir, output)
    n_cpu = max(1, cpus // chunks)

    # Step 1. IF GFF or GTF is provided, make it into a genome-based fasta
    #         IF sequence is provided, align as SAM then correct with genome
    if os.path.exists(corrFASTA):
        print("Error corrected FASTA {0} already exists. Using it...".format(corrFASTA), file=sys.stderr)
    else:
        print("Correcting fasta")
        if fasta:
            if os.path.exists(corrSAM):
                print("Aligned SAM {0} already exists. Using it...".format(corrSAM), file=sys.stderr)
            else:
                cmd = get_aligner_command(aligner_choice, genome, isoforms, annotation, 
                                          outdir,corrSAM, n_cpu, gmap_index)
                run_command(cmd, description="aligning reads")

            # error correct the genome (input: corrSAM, output: corrFASTA)
            err_correct(genome, corrSAM, corrFASTA, genome_dict=genome_dict)
            # convert SAM to GFF --> GTF
            convert_sam_to_gff3(corrSAM, corrGTF+'.tmp', source=os.path.basename(genome).split('.')[0])  # convert SAM to GFF3
        else:
            print("Skipping aligning of sequences because GTF file was provided.", file=sys.stdout)
            filter_gtf(isoforms, corrGTF+'.tmp', badstrandGTF, genome_dict)

            if not os.path.exists(corrSAM):
                sys.stdout.write("\nIndels will be not calculated since you ran SQANTI3 without alignment step (SQANTI3 with gtf format as transcriptome input).\n")

            # GTF to FASTA
            subprocess.call([GFFREAD_PROG, corrGTF+'.tmp', '-g', genome, '-w', corrFASTA])
        cmd = "{p} {o}.tmp -T -o {o}".format(o=corrGTF, p=GFFREAD_PROG)
        # Try condition to better handle the error. Also, the exit code is corrected
        run_command(cmd, description="converting GFF3 to GTF")
        os.remove(corrGTF+'.tmp')

def process_gtf_line(line: str, genome_dict: Dict[str, str], corrGTF_out: str, discard_gtf: str):
    """
    Processes a single line from a GTF file, validating and categorizing it based on certain criteria.

    Args:
        line (str): A single line from a GTF file.
        genome_dict (Dict[str, str]): A dictionary containing genome reference data, where keys are chromosome names.
        corrGTF_out (str): Path to a file to write valid GTF lines with known strand information.
        discard_gtf (str): Path to a file to write GTF lines with unknown strand information.
    Raises:
        ValueError: If the chromosome in the GTF line is not found in the genome reference dictionary.

    Notes:
        - Lines starting with '#' are ignored.
        - Lines with fewer than 7 fields are considered malformed and skipped with a warning.
        - Lines with 'transcript' or 'exon' feature types are further processed:
            - If the strand is unknown ('-' or '+'), the line is written to the discard_gtf file with a warning.
            - Otherwise, the line is written to the corrGTF_out file.
    """
    if line.startswith("#"):
        return

    fields = line.strip().split("\t")
    if len(fields) < 7:
        print(f"WARNING: Skipping malformed GTF line: {line.strip()}")
        return

    chrom, feature_type, strand = fields[0], fields[2], fields[6]

    if chrom not in genome_dict:
        raise ValueError(f"ERROR: GTF chromosome '{chrom}' not found in genome reference file.")

    if feature_type in ('transcript', 'exon'):
        if strand not in ['-', '+']:
            print(f"WARNING: Discarding unknown strand transcript: {line.strip()}")
            discard_gtf.write(line)
        else:
            corrGTF_out.write(line)

def filter_gtf(isoforms: str, corrGTF, badstrandGTF, genome_dict: Dict[str, str]) -> None:
    try:
        with open(corrGTF, 'w') as corrGTF_out, open(isoforms, 'r') as isoforms_gtf, open(badstrandGTF, 'w') as discard_gtf:
            for line in isoforms_gtf:
                process_gtf_line(line, genome_dict, corrGTF_out, discard_gtf)
    except IOError as e:
        print(f"ERROR: Error processing GTF files: {e}")
        raise


def predictORF(outdir, skipORF,orf_input , corrFASTA, corrORF):
    # ORF generation
    print("**** Predicting ORF sequences...", file=sys.stdout)

    gmst_dir = os.path.join(os.path.abspath(outdir), "GMST")
    gmst_pre = os.path.join(gmst_dir, "GMST_tmp")
    if not os.path.exists(gmst_dir):
        os.makedirs(gmst_dir) 

    # sequence ID example: PB.2.1 gene_4|GeneMark.hmm|264_aa|+|888|1682
    gmst_rex = re.compile(r'(\S+\t\S+\|GeneMark.hmm)\|(\d+)_aa\|(\S)\|(\d+)\|(\d+)')
    # GMST seq id --> myQueryProteins object
    orfDict = {}
    if skipORF:
        print("WARNING: Skipping ORF prediction because user requested it. All isoforms will be non-coding!", file=sys.stderr)
    elif os.path.exists(corrORF):
        print(f"ORF file {corrORF} already exists. Using it....", file=sys.stderr)
        orfDict = parse_corrORF(corrORF,gmst_rex)
    else:
        print(f"Running ORF prediction on {corrFASTA}")
        run_gmst(corrFASTA,orf_input,gmst_pre)
        # Modifying ORF sequences by removing sequence before ATG
        orfDict = parse_GMST(corrORF, gmst_rex, gmst_pre)
    if len(orfDict) == 0:
        print("WARNING: All input isoforms were predicted as non-coding", file=sys.stderr)

    return(orfDict)


def rename_novel_genes(isoform_info,novel_gene_prefix=None):
    """
    Rename novel genes to be "novel_X" where X is a number
    """
    novel_gene_index= 1
    for isoform_hit in isoform_info.values():
        if isoform_hit.str_class in ("intergenic", "genic_intron"):
            # Liz: I don't find it necessary to cluster these novel genes. They should already be always non-overlapping.
            prefix = f'novelGene_{novel_gene_prefix}_' if novel_gene_prefix is not None else 'novelGene_'
            isoform_hit.genes = [f'{prefix}{novel_gene_index}']
            isoform_hit.transcripts = ['novel']
            novel_gene_index += 1
    return isoform_info