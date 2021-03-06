"""
Launch HC to compute the reassembled bam.
"""
import datetime
import getpass
import itertools
import logging
import os
import subprocess

from utils.postprocess_reassembled_bam import postprocess_bam
from utils.check_gvcf import check_gvcf
from utils.database import Sample
from utils.exac_calling_intervals import get_adjacent_calling_intervals
from utils.constants import NUM_OUTPUT_DIRECTORIES_L1, INCLUDE_N_ADJACENT_CALLING_REGIONS, MAX_ALLELE_SIZE, GATK_JAR_PATH
from utils.file_utils import does_file_exist, retry_if_IOError

from utils.constants import TCGA_NEW_BAM_PATHS

# error codes
ERROR_ORIGINAL_BAM_NOT_FOUND = 1000
ERROR_HC_CRASHED = 2000
ERROR_GVCF_MISMATCH = 3000
ERROR_REASSEMBLED_BAM_IS_EMPTY = 4000

MAX_LINUX_FILENAME_LENGTH = 260


def run_haplotype_caller(
        chrom,
        pos,
        ref,
        alt,
        het_or_hom_or_hemi,
        original_bam_path,
        original_gvcf_path,
        sample_id,
        sample_i,
        all_bam_output_dir = None,
        only_choose_samples = False,
    ):
    """Runs HC and does pre/post-processing on the given variant.

    Args:
        chrom: chromosome
        ...
        het_or_hom_or_hemi: "het" or "hom" to indicate whether this sample was originally called as HET or HOM
        original_bam_path: full path of BAM used as input in the original HC run
        original_gvcf_path:  full path of GVCF generated during the original HC run
        sample_id: vcf sample id
        sample_i: if this sample passes all criteria, it would be sample number i to be shown for this variant
        all_bam_output_dir: top-level output dir for all reassembled bams
        only_choose_samples: if True, then don't actually run haplotype caller. just
    Return:
        2-tuple (x,y) where
            x = True if HC succeeded (or False otherwise)
            y = the reassembled bam path (or None)
    """

    # if finished already, just return
    sr, created = Sample.get_or_create(
        chrom=chrom,
        pos=pos,
        ref=ref,
        alt=alt,
        het_or_hom_or_hemi=het_or_hom_or_hemi,
        sample_id=sample_id)

    output_bam_path = compute_output_bam_path(chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i)
    if sr.finished and sr.output_bam_path == output_bam_path:
        #logging.info("%s-%s-%s-%s - %s - already done " % (chrom, pos, ref, alt, sample_id))
        return (sr.hc_succeeded, sr.output_bam_path)

    sr.variant_id = "%s-%s-%s-%s" % (chrom, pos, ref, alt)
    sr.sample_i = sample_i
    sr.original_bam_path = str(original_bam_path)
    if sample_id in TCGA_NEW_BAM_PATHS or "tcga" in original_bam_path.lower():
        sr.priority = 1

    if not only_choose_samples:
        sr.username = getpass.getuser()[0:10]
        sr.started = 1
        sr.comments = str(sr.comments or "")+"_s"  # started - used to check that started only once
        sr.started_time = datetime.datetime.now()
    sr.save()

    logging.info("%s-%s-%s-%s %s - %s %s - start " % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id))
    # TODO check if original bam path in set of missing bams (pre-compute missing files cache)
    if not only_choose_samples and not does_file_exist(sr.original_bam_path):
        logging.info("%s-%s-%s-%s %s - %s %s - %s: %s" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id, ".bam not found", original_bam_path))
        error_text = "BAM not found"
        if not only_choose_samples:
            sr.finished = 1
            hc_failed(ERROR_ORIGINAL_BAM_NOT_FOUND, error_text, sr)
        else:
            sr.hc_error_code = ERROR_ORIGINAL_BAM_NOT_FOUND
            sr.hc_error_text = error_text
            sr.save()

        return (False, None)

    # look up the exac calling interval that spans this variant, as well as its 2 adjacent intervals on either side
    left_i, i, right_i = get_adjacent_calling_intervals(
                            chrom,
                            pos,
                            n_left=INCLUDE_N_ADJACENT_CALLING_REGIONS,
                            n_right=INCLUDE_N_ADJACENT_CALLING_REGIONS)

    assert chrom == i.chrom, "%s chrom doesn't match %s" % (str(i), chrom)

    sr.calling_interval_start = i.start
    sr.calling_interval_end = i.end
    sr.original_gvcf_path = str(original_gvcf_path)

    # first, output to temp files to avoid partially-finished files if HC crashes or is killed
    relative_output_dir = os.path.dirname(output_bam_path)
    temp_output_bam_path = os.path.join(all_bam_output_dir, relative_output_dir, "tmp." + os.path.basename(output_bam_path))

    files_to_delete_on_error = [temp_output_bam_path]
    temp_output_gvcf_path = os.path.join(all_bam_output_dir, relative_output_dir, "tmp." + os.path.basename(output_bam_path.replace(".bam", "") + ".gvcf"))
    files_to_delete_on_error += [temp_output_gvcf_path, temp_output_gvcf_path+".idx"]

    # make sure output directory exists
    absolute_output_dir = os.path.dirname(temp_output_bam_path)
    if not os.path.isdir(absolute_output_dir):
        logging.debug("creating directory: %s" % absolute_output_dir)
        run("mkdir -p %(absolute_output_dir)s; chmod 777 %(absolute_output_dir)s %(absolute_output_dir)s/.. " % locals())

    dash_L_intervals = list(itertools.chain.from_iterable(
        [('-L', str(interval)) for interval in left_i + [i] + right_i]))

    # see https://www.broadinstitute.org/gatk/guide/article?id=5484  for details on using -bamout
    gatk_cmd = [
       "java",
        "-XX:+UseSerialGC",
        "-XX:+ReduceSignalUsage",
        "-XX:+UseSerialGC",
        "-XX:CICompilerCount=1",
        "-XX:+DisableAttachMechanism",
        "-XX:MaxHeapSize=1000m",
        #'-jar', './gatk-protected/target/executable/GenomeAnalysisTK.jar',
        "-Xmx7500m",
        '-jar', GATK_JAR_PATH,
        '-T', 'HaplotypeCaller',
        '-R', "/seq/references/Homo_sapiens_assembly19/v1/Homo_sapiens_assembly19.fasta",
        '--disable_auto_index_creation_and_locking_when_reading_rods',
        '-stand_call_conf', '30.0',
        '-stand_emit_conf', '30.0',
        '--minPruning', '3',
        '--maxNumHaplotypesInPopulation', '200',
        '-ERC', 'GVCF',
        '--max_alternate_alleles', '3',
        # '-A', 'DepthPerSampleHC',
        # '-A', 'StrandBiasBySample',
        #'--forceActive',
        '--variant_index_type', 'LINEAR',
        '--variant_index_parameter', '128000',
        '--paddingAroundSNPs', ' 300',
        '--paddingAroundIndels', '300',
        '-I', original_bam_path,
        '-bamout', temp_output_bam_path,
        '--disable_bam_indexing',
        '-o', temp_output_gvcf_path,
        #'-et', 'NO_ET',

    ] + list(dash_L_intervals)

    sr.hc_command_line = " ".join(gatk_cmd)
    sr.save()

    if only_choose_samples:
        logging.info("%s-%s-%s-%s %s - %s %s - %s" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id, " finished choosing sample"))
        return (True, output_bam_path)

    try:
        logging.info("%s-%s-%s-%s %s - %s %s - launching HC" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id))
        logging.info(sr.hc_command_line)
        #os.system(" ".join(gatk_cmd))
        cmd_output = subprocess.check_output(sr.hc_command_line, stderr=subprocess.STDOUT, shell=True).decode()
        logging.info("Output:\n"+cmd_output)
        if "Total runtime" not in cmd_output:
            raise subprocess.CalledProcessError(100, sr.hc_command_line, cmd_output)
    except subprocess.CalledProcessError as e:
        error_message = ("%s\n"
            "return code: %s\n"
            "output: %s") % (sr.hc_command_line, e.returncode, e.output.strip())
        # add the return code to the ERROR_CODE so that different types of crashes have a different error code
        hc_failed(ERROR_HC_CRASHED + abs(e.returncode) % 500, error_message, sr, files_to_delete_on_error)
        logging.error("ERROR: HC failed: return code %s." % e.returncode)
        logging.error("ERROR: GATK output:")
        logging.error("\t %s" % sr.hc_error_text)
        return (False, None)

    # check GVCF against original GVCF call
    sr.is_missing_original_gvcf = not does_file_exist(sr.original_gvcf_path) or not does_file_exist(sr.original_gvcf_path + ".tbi")

    if not sr.is_missing_original_gvcf:
        logging.info("%s-%s-%s-%s %s - %s %s - checking gvcfs" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id))
        gvcf_calls_matched, mismatch_error_code, mismatch_error_text = retry_if_IOError(
            check_gvcf, sr.original_gvcf_path, temp_output_gvcf_path, chrom, pos)

        if not gvcf_calls_matched:
            sr.finished = 1
            error_code = ERROR_GVCF_MISMATCH + mismatch_error_code  # combine the 2 error codes
            hc_failed(error_code, mismatch_error_text, sr)

            logging.info("%s-%s-%s-%s %s - %s %s - gvcfs mimatch: %s - %s" % (
                chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id, error_code, mismatch_error_text))

            # save the output gvcf for debugging
            absolute_debug_dir = os.path.join(all_bam_output_dir, "debug", relative_output_dir)
            if not os.path.isdir(absolute_debug_dir):
                run("mkdir -p %(absolute_debug_dir)s; chmod 777 %(absolute_debug_dir)s %(absolute_debug_dir)s/.." % locals())

            igv_tracks = []
            for file_to_save_for_debugging in files_to_delete_on_error:
                destination_path = os.path.join(absolute_debug_dir, os.path.basename(file_to_save_for_debugging).replace("tmp.", ""))
                run("mv -f %s %s" % (file_to_save_for_debugging, destination_path))
                if destination_path.endswith(".bam"):
                    igv_tracks.append(destination_path)

            # create symlinks to original bam and gvcf
            symlink_path = os.path.join(absolute_debug_dir, os.path.basename(temp_output_bam_path).replace(".bam", "").replace("tmp.", "") + ".original.bam")
            run("ln -s -f %s %s" % (original_bam_path, symlink_path))
            run("ln -s -f %s %s" % (original_bam_path.replace(".bam", ".bai"), symlink_path + ".bai"))

            symlink_path = os.path.join(absolute_debug_dir, os.path.basename(temp_output_bam_path).replace(".bam", "").replace("tmp.", "") + ".original.gvcf.gz")
            run("ln -s -f %s %s" % (original_gvcf_path, symlink_path))
            run("ln -s -f %s %s" % (original_gvcf_path + ".tbi", symlink_path + ".tbi"))

            # no more screenshots needed
            #take_screenshots(chrom, pos, igv_tracks, absolute_debug_dir)

            return (False, None)

    run("rm -f %s" % temp_output_gvcf_path)
    run("rm -f %s" % (temp_output_gvcf_path+".idx"))

    logging.info("%s-%s-%s-%s %s - %s %s - post-processing bams" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id))
    # postprocess and move output bam from temp_output_bam_path to output_bam_path
    # strip out read groups, read ids, tags, etc. to remove any sensitive info and reduce bam size
    final_output_bam_path = os.path.join(all_bam_output_dir, output_bam_path)

    run("rm -f %s" % final_output_bam_path)
    (is_reassembled_bam_empty, sr.hc_n_artificial_haplotypes, sr.hc_n_artificial_haplotypes_deleted) = retry_if_IOError(
        postprocess_bam, temp_output_bam_path, final_output_bam_path, chrom, pos, ref, alt)

    run("rm -f %s" % temp_output_bam_path)
    run("chmod 666 %s" % final_output_bam_path)  # in case different users run this script

    if is_reassembled_bam_empty:
        logging.info("%s-%s-%s-%s - %s - %s" % (chrom, pos, ref, alt, sample_id, "reassembled bam is empty"))

        files_to_delete_on_error.append( final_output_bam_path )
        files_to_delete_on_error.append( final_output_bam_path+".bai" )
        sr.finished = 1
        hc_failed(ERROR_REASSEMBLED_BAM_IS_EMPTY, "reassembled bam is empty", sr, files_to_delete_on_error)
        return (False, None)
    else:
        pass
        #run("mv -f %s %s" % (temp_output_bam_path.replace(".bam", ".bai"), final_output_bam_path.replace(".bam", ".bai")))
        #run("samtools index %s" % output_bam_path)

    sr.comments = str(sr.comments or "") + "_succeeded"
    sr.finished = 1
    sr.finished_time = datetime.datetime.now()
    sr.output_bam_path = output_bam_path
    sr.sample_i = sample_i
    sr.hc_succeeded = 1
    sr.save()

    logging.info("%s-%s-%s-%s %s - %s %s - %s" % (chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, sample_id, "done!"))
    return (True, sr.output_bam_path)


def compute_output_bam_path(chrom, pos, ref, alt, het_or_hom_or_hemi, sample_i, suffix=""):
    """Computes the reassembled bam output path"""

    output_dir = "%s/%03d" % (chrom, pos % NUM_OUTPUT_DIRECTORIES_L1) # pos % NUM_OUTPUT_DIRECTORIES_L2)

    max_allele_size = min(MAX_ALLELE_SIZE, MAX_LINUX_FILENAME_LENGTH - 40)

    output_bam_filename = "chr%s-%s-%s-%s_%s%s%s.bam" % (
        chrom,
        pos,
        ref[:max_allele_size],
        alt[:max_allele_size],
        het_or_hom_or_hemi,
        sample_i,
        suffix)

    return os.path.join(output_dir, output_bam_filename)


def run(command, verbose=False):
    """Utility method to execute a shell command"""
    if verbose:
        logging.info(command)
    subprocess.call(command, shell=True)


def hc_failed(error_code, message, sample_record, files_to_delete=None):
    """Utility method for logging HC run failure"""
    sample_record.hc_failed = 1
    sample_record.finished_time = datetime.datetime.now()
    sample_record.hc_error_code = error_code
    sample_record.hc_error_text = message
    sample_record.output_bam_path = None
    sample_record.comments = str(sample_record.comments or "") + "_error"+str(error_code)
    sample_record.save()

    if files_to_delete:
        for path in files_to_delete:
            if os.path.isfile(path):
                run("rm -f %s" % path)
