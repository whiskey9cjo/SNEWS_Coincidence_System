
import json, os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from time import sleep
from .core.logging import getLogger
from .cs_email import send_warning_mail, send_feedback_mail
from .snews_hb import beats_path, mirror_csv

log = getLogger(__name__)

contact_list_file = os.path.abspath(os.path.join(os.path.dirname(__file__), 'auxiliary/contact_list.json'))
with open(contact_list_file) as file:
    contact_list = json.load(file)

# Check if detector name is in registered list.
detector_file = os.path.abspath(os.path.join(os.path.dirname(__file__), 'auxiliary/detector_properties.json'))
with open(detector_file) as file:
    snews_detectors = json.load(file)
snews_detectors = list(snews_detectors.keys())

# verbose print. Prints only if verbose=True
vprint = lambda inp, _bool: print(inp) if _bool else None

class FeedBack:
    """ Once every minute, check the HB of each detector
        If the last heartbeat is from longer than usual, send an email
        Once every user-defined time interval, send a plot with latency and frequency statistics

    """
    def __init__(self, verbose=False):
        self.detectors = snews_detectors
        self.last_feedback_time = dict()
        for k in self.detectors:
            self.last_feedback_time[k] = datetime(2022, 1, 1)
        self.day_in_min = 1440
        self.running_min = 0
        self.verbose = verbose
        log.info(f"\t> Heartbeat tracking initiated.")

    def __call__(self):
        """ Continuously run and check expected heartbeats every minute
            Also, check if the detectors requested feedbacks
            create and send feedbacks with the desired time intervals
        """
        while True:
            # run every minute
            sleep(60)
            try:
                df = pd.read_csv(mirror_csv, parse_dates=['Received Times'], )
            except FileNotFoundError:
                log.error(f"{mirror_csv} does not exist yet! Maybe `snews_cs run-coincidence` is not invoked?")
                while not os.path.isfile(mirror_csv):
                    sleep(60)
                df = pd.read_csv(mirror_csv, parse_dates=['Received Times'], )
                log.debug(f"OK {mirror_csv} found! Moving on")

            self.control(df) # check if a detector is taking longer than usual (mean+3*sigma>)
            self.running_min += 1
            vprint(f"[DEBUG] >>>>> Running minute: {self.running_min}", self.verbose)
            # every hour, reset the minute counter, increase hour counter
            # and check if it has been feedback time for any detector.
            if (self.running_min % 60) == 0:
                self.running_min = 0  # reset the counter
                delete_old_figures()


    def control(self, df):
        """ Check the current cache, check if any detector
            missed a beat

        """
        # get the heartbeats of this detector from last 24 hours
        last24hours = (datetime.utcnow() - timedelta(hours=24))
        data = df[df['Received Times'] > last24hours]
        data.sort_values('Received Times', inplace=True)

        for detector in data['Detector'].unique():
            detector_df = data.query('Detector==@detector')
            # For a given detector, if already sent an email,
            # ignore the beats before that email. Otherwise, the same cause would ruin the statistics.
            after_last_hb =  self.last_feedback_time[detector]
            detector_df = detector_df[detector_df['Received Times'] > after_last_hb]
            detector_df.sort_values('Received Times', inplace=True)

            if len(detector_df) < 5:
                # not enough statistics, skip.
                vprint(f"[DEBUG] >>>>> len {len(detector_df)} Not enough!", self.verbose)
                continue
            # check if a heartbeat is skipped
            self.check_missed_beats(detector_df, detector)

    def check_missed_beats(self, df, detector):
        """ Check if a heartbeat is skipped

        """
        vprint("\n[DEBUG] >>>>> Checking if beat skipped", self.verbose)
        # get the computed delays between 2 consecutive hb
        mean = np.mean(df['Time After Last'])
        std = np.std(df['Time After Last'])

        last_hb = df['Received Times'].values[-1] # this is a numpy.datetime
        last_hb = pd.to_datetime(last_hb)         # we have to convert it to datetime.datetime
        since_lasthb = datetime.utcnow() - last_hb
        vprint(f"[DEBUG] >>>>> mean:{mean:.2f}, std:{std:.2f}, trigger at {mean + 3 * std:.2f}", self.verbose)
        vprint(f"[DEBUG] >>>>> Delay since last: {since_lasthb.total_seconds():.2f}", self.verbose)
        if since_lasthb > timedelta(seconds=(mean + 3 * std)):
            # something is wrong!
            if last_hb == self.last_feedback_time[detector]:
                # this warning has already been sent! Skip it
                return None
            expected_hb = last_hb + timedelta(seconds=float(mean))  # +/- std
            text = f" Your -{detector}- heartbeat frequency is every {mean:.2f}+/-{std:.2f} sec. " \
                   f" Expected a heartbeat at {expected_hb.isoformat()} +/- {std:.2f} sec. " \
                   f" Since last heartbeat there has been {since_lasthb.total_seconds():.2f} sec. " \
                   f" Is everything alright? Do you wanna talk about it?"
            vprint(f"[DEBUG] >>>>> Warning for {detector} is created, trying to send.", self.verbose)
            # send warning to detector
            send_warning_mail(detector, text)
            self.last_feedback_time[detector] = last_hb
        return None

    def check_enough_detectors(self):
        """ Constantly check to make sure there is at least two
            detector taking data. If not, send a warning to everyone.
        """
        pass

def check_frequencies_and_send_mail(detector, given_contact=None):
    """ Create a plot with latency and heartbeat frequencies
        and send it via emails
    """
    df = pd.read_csv(mirror_csv, parse_dates=['Received Times'], )
    df.query("Detector==@detector", inplace=True)
    now_str = datetime.utcnow().strftime("%Y-%m-%d_%HH%MM")
    mean = np.mean(df['Time After Last'])
    std = np.std(df['Time After Last'])
    try:
        last_hb = df['Received Times'].values[-1]  # this is a numpy.datetime
    except Exception as e:
        log.debug(f"> Frequency check failed for {detector}, probably no beats within last 24h\n{e}")
        fail_text = f"Could not find any entries within last 24hours!"
        out = send_feedback_mail(detector, None, fail_text, given_contact=given_contact)
        return "-No Attachment Created, Warned-", out

    last_hb = pd.to_datetime(last_hb)  # we have to convert it to datetime.datetime
    text = f" Your heartbeat frequency is every {mean:.2f}+/-{std:2f} sec." \
           f" The last heartbeat received at {last_hb}. " \
           f" The received heartbeat frequency, together with the computed latency" \
           f" is plotted, and sent in the attachment."

    attachment = f"{detector}_{now_str}.png"
    plot_beats(df, detector, attachment)  # create a plot to send
    out = send_feedback_mail(detector, attachment, text, given_contact=given_contact)
    return attachment, out


def plot_beats(df, detector, figname):
    """ Requires QT libraries: sudo apt-get install qt5-default
    """
    latency = pd.to_timedelta(df['Latency'].values).total_seconds()
    received_times = df['Received Times']
    try:
        unique_days = list(set([datetime.strptime(date, '%Y-%m-%d %H:%M:%S.%f').strftime('%Y-%m-%d') for date in received_times]))
    except Exception as e:
        log.debug(f"> Received times might be datetime object \t{e}")
        unique_days = list(set([date.strftime('%Y-%m-%d') for date in received_times]))
    if len(unique_days) > 1:
        date = "&".join([i for i in unique_days])
    else:
        date = list(unique_days)[0]

    xticklabels, xticks_positions = [], []
    _first = received_times.iloc[0]
    _last = received_times.iloc[-1]
    date_ranges = pd.date_range(_first, _last, 10)

    for date in date_ranges:
        try:
            dt = datetime.strptime(date, '%Y-%m-%d %H:%M:%S.%f')
        except Exception as e:
            log.debug(f"> Received times might be datetime object \t{e}")
            dt = date
        time_str = dt.strftime('%H:%M:%S')
        xticks_positions.append(dt)
        xticklabels.append(time_str)

    time_after_last = df['Time After Last'].astype(float)
    mean = np.mean(time_after_last)
    std = np.std(time_after_last)

    fig, (ax1, ax2) = plt.subplots(nrows=2, figsize=(17, 7), sharex=True)
    plt.subplots_adjust(hspace=0.05)
    
    ax1.set_title(f"HeartBeat data for {detector}, {date}", fontsize=20)
    ax1.fill_between(received_times, mean - 3 * std, mean + 3 * std, alpha=0.5, color='aqua')
    ax1.fill_between(received_times, mean - std, mean + std, alpha=1, color='darkturquoise')
    ax1.axhline(mean, label=f'mean freq:{mean:.2f} sec', color='0.5', ls='--')
    colors = ['yellowgreen' if i == 'ON' else 'crimson' for i in df['Status']]
    ax1.plot(received_times, time_after_last, color='k', zorder=1)
    ax1.scatter(received_times, time_after_last, marker='o', c=colors, ec='k', s=time_after_last*50, zorder=20)
    ax1.set_ylabel("Frequency\nSeconds after last", fontsize=18)

    ax2.axhline(np.mean(latency), color='darkred', alpha=0.7, ls='--')
    ax2.plot(received_times, latency, zorder=1, color='k', ls='-', label=f'mean latency:{np.mean(latency):.2f} sec')
    ax2.scatter(received_times, latency, marker='o', c=latency, cmap='Wistia', ec='b', s=3.2 ** latency, zorder=20)
    ax2.set_ylabel('Latency [sec]', color='k', fontsize=18)
    ax2.set_xlabel("Received Times", fontsize=18)
    # xticks_positions, _ = plt.xticks()
    ax2.set_xticks(xticks_positions, xticklabels)
    ax2.tick_params(axis='x', labelsize=18)
    ax1.tick_params(axis='y', labelsize=18)
    ax2.tick_params(axis='y', labelsize=18)
    ax2.set_ylim(0, np.max([8, np.max(latency)]))

    ax1.legend(loc='upper right', fontsize=18); ax2.legend(loc='upper right', fontsize=18)
    plt.savefig(os.path.join(beats_path, figname))

def delete_old_figures():
    """ Remove the old feedback figures from the server
        the duration set in the configuration file
    """
    delete_after = timedelta(days=int(os.getenv("REMOVE_FIGURES_AFTER")))
    now = datetime.utcnow()

    # the times of existing figures
    existing_figures = os.listdir(beats_path)
    existing_figures = np.array([x for x in existing_figures if x.endswith('.png')])
    # take only dates
    dates_str = ["_".join(i.split('/')[-1].split("_")[1:]).split('.png')[0] for i in existing_figures]
    dates, files = [], []
    for d_str, logfile in zip(dates_str, existing_figures):
        try:
            dates.append(datetime.strptime(d_str, "%Y-%m-%d_%HH%MM"))
            files.append(logfile)
        except Exception as e:
            log.error(f"\t> Something went wrong during deletion of old figures \n\t{e}")
            continue

    time_differences = np.array([date - now for date in dates])
    older_than_limit = np.where(np.abs(time_differences) > delete_after)
    files = np.array(files)
    log.debug(f"\t> The following feedback figures are older than "
              f"{int(os.getenv('REMOVE_FIGURES_AFTER'))} days and will be removed; "
              f"\n\t{files[older_than_limit[0]]}")
    for file in files[older_than_limit[0]]:
        filepath = os.path.join(beats_path, file)
        os.remove(filepath)
        log.debug(f"\t> {file} deleted.")
