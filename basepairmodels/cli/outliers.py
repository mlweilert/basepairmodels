import json
import logging
import numpy as np
import os
import pandas as pd
import pyBigWig

from basepairmodels.cli.argparsers import outliers_argsparser
from basepairmodels.cli.exceptionhandler import NoTracebackException
from basepairmodels.cli import logger
from tqdm import tqdm

def getPeakPositions(task, chroms, chrom_sizes, flank, drop_duplicates=False):
    """ 
        Peak positions for given task filtered based on required
        chromosomes and other qc filters. 
        
        Args:
            tasks (dict): A python dictionary containing the task
                information for a single task
            chroms (list): The list of required chromosomes
            chrom_sizes (pandas.Dataframe): dataframe of chromosome 
                sizes with 'chrom' and 'size' columns
            flank (int): Buffer size before & after the position to  
                ensure we dont fetch values at index < 0 & > chrom size
            drop_duplicates (boolean): True if duplicates should be
                dropped from returned dataframe. 
            
        Returns:
            pandas.DataFrame: 
                pruned dataframe of peak positions 
            
    """

    # necessary for dataframe apply operation below --->>>
    chrom_size_dict = dict(chrom_sizes.to_records(index=False))

    # initialize an empty dataframe
    allPeaks = pd.DataFrame()

    # we concatenate all the peaks from list of peaks files
    for peaks_file in task['loci']['source']:

        peaks_df = pd.read_csv(
            peaks_file, sep='\t', header=None, 
            names=['chrom', 'st', 'e', 'name', 'weight', 'strand', 
                   'signal', 'p', 'q', 'summit'])

        # keep only those rows corresponding to the required 
        # chromosomes
        peaks_df = peaks_df[peaks_df['chrom'].isin(chroms)]

        # create new column for peak pos
        peaks_df['pos'] = peaks_df['st'] + peaks_df['summit']

        # compute left flank coordinates of the input sequences 
        # (including the allowed jitter)
        peaks_df['start'] = (peaks_df['pos'] - flank).astype(int)

        # compute right flank coordinates of the input sequences 
        # (including the allowed jitter)
        peaks_df['end'] = (peaks_df['pos'] + flank).astype(int)

        # filter out rows where the left flank coordinate is < 0
        peaks_df = peaks_df[peaks_df['start'] >= 0]

        # --->>> create a new column for chrom size
        peaks_df["chrom_size"] = peaks_df['chrom'].apply(
            lambda chrom: chrom_size_dict[chrom])

        # filter out rows where the right flank coordinate goes beyond
        # chromosome size
        peaks_df = peaks_df[
            peaks_df['end'] <= peaks_df['chrom_size']]

        # sort based on chromosome number and right flank coordinate
        peaks_df = peaks_df.sort_values(
            ['chrom', 'end']).reset_index(drop=True)

        # append to all peaks data frame
        allPeaks = allPeaks.append(peaks_df[
            ['chrom', 'st', 'e', 'start', 'end', 'name', 'weight', 'strand', 
             'signal', 'p', 'q', 'summit']])

        allPeaks = allPeaks.reset_index(drop=True)
    
    # drop the duplicate rows, i.e. the peaks that get duplicated
    # for the plus and minus strand tasks
    if drop_duplicates:
        allPeaks = allPeaks.drop_duplicates(ignore_index=True)
        
    return allPeaks


def outliers_main():
    
    # parse the command line arguments
    parser = outliers_argsparser()
    args = parser.parse_args()
    
    # filename to write debug logs
    logfname = "outliers.log"
    
    # set up the loggers
    logger.init_logger(logfname)
    
    # check if the input json file exists
    if not os.path.exists(args.input_data):
        raise NoTracebackException(
            "Directory {} does not exist".format(args.input_data))

    # check if the chrom sizes file exists
    if not os.path.exists(args.chrom_sizes):
        raise NoTracebackException(
            "Directory {} does not exist".format(args.chrom_sizes))

    chrom_sizes_df = pd.read_csv(
            args.chrom_sizes, sep='\t', header=None, names=['chrom', 'size'])

    # load the json file exists
    with open(args.input_data, 'r') as inp_json:
        try:
            tasks = json.loads(inp_json.read())
        except json.decoder.JSONDecodeError:
            raise NoTracebackException(
                "Unable to load json file {}. Valid json expected. "
                "Check the file for syntax errors.".format(
                    tasks_json))
    
    # get all peaks with start and end coordinates in a dataframe
    peaks_df = getPeakPositions(
        tasks[args.task], args.chroms, chrom_sizes_df, args.sequence_len // 2, 
        drop_duplicates=True)
    
    # open all the signal bigWigs for reading
    signal_files = []
    for signal_file in tasks[args.task]['signal']['source']:
        # check if the bigWig file exists
        if not os.path.exists(signal_file):
            raise NoTracebackException(
                "BigWig file {} does not exist".format(signal_file))
                
        signal_files.append(pyBigWig.open(signal_file))

    # iterate through all peaks and read values from the bigWig files
    counts = {}
    for signal_file in signal_files:
        counts[signal_file] = []
    
    logging.info("Computing counts for each peak")
    for _, row in tqdm(peaks_df.iterrows(), desc='peaks', total=len(peaks_df)):
        chrom = row['chrom']
        start = row['start']
        end = row['end']
        
        for signal_file in signal_files:
            counts[signal_file].append(
                np.sum(np.nan_to_num(signal_file.values(chrom, start, end))))
                
    # add a new counts column to the peaks dataframe
    for signal_file in signal_files:
        peaks_df[signal_file] = counts[signal_file]
    
    peaks_df['avg_counts'] = peaks_df[signal_files].mean(axis=1)
    
    peaks_df = peaks_df.sort_values(by=['avg_counts'])
    
    counts = peaks_df['avg_counts'].values            
                
    print(counts)

    # compute the quantile value
    nth_quantile = np.quantile(counts, args.quantile)
    logging.info("{} quantile {}".format(args.quantile, nth_quantile))
    
    # get index of quantile value 
    quantile_idx = abs(counts - nth_quantile).argmin()
    logging.info("quantile idx {}".format(quantile_idx))
                
    # scale value at quantile index
    scaled_value = counts[quantile_idx] * args.quantile_value_scale_factor
    logging.info("scaled_value {}".format(scaled_value))

    # index of values greater than scaled_value
    max_idx = np.argmax(counts > scaled_value)
    logging.info("max_idx {}".format(max_idx))    
        
    # trimmed data frame with outliers removed
    logging.info("original size {}".format(len(peaks_df)))    
    peaks_df = peaks_df[:max_idx]
    logging.info("new size {}".format(len(peaks_df)))    
                
    # save the new dataframe
    logging.info("Saving output bed file ... {}".format(args.output_bed))
    peaks_df = peaks_df[['chrom', 'st', 'e', 'name', 'weight', 
                         'strand', 'signal', 'p', 'q', 'summit']]
    peaks_df.to_csv(args.output_bed, header=None, sep='\t', index=False)
                
if __name__ == '__main__':
    outliers_main()

