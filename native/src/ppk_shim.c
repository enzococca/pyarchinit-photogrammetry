/*------------------------------------------------------------------------------
* ppk_shim.c : RTKLIB's post-processing (rnx2rtkp -k) and its conversion of raw
*              receiver logs into RINEX (convbin) as functions of ppg_core
*
* RTKLIB (native/vendor/rtklib, BSD 2-clause, unchanged) asks the program it is
* built into for three callbacks (showmsg, settspan, settime) and keeps its state
* in globals. ppg_ppk() sets that state before every run, so it can run again in
* the same process; it is not reentrant: the caller (ppk.rs) holds a lock.
*
* postpos.c is compiled here, in the same translation unit, not on its own: its
* file-scope counters of the invalid time marks (nitm, iitm) are never reset by
* RTKLIB, and from the second run on they would put the time marks of an earlier
* run into the events file of the next one. Including it is the only way to
* reach them without changing RTKLIB's files.
*
* RTKLIB reads the options file and the RINEX files, and writes the solution, with the C library's
* locale-dependent functions (atof, sscanf "%lf", printf "%f"). Under a locale with a decimal comma
* (an Italian, German or French system's, which any library that calls setlocale() can leave in the
* process) it reads "1.5" as 1 and writes "1,5": the run computes nothing and reports no error.
* ppg_ppk() therefore runs RTKLIB in the "C" locale of the calling thread alone (POSIX uselocale(),
* the per-thread locale of the Windows C runtime) and puts back the locale the thread had before it
* returns. The locale of the process, and the other threads, are not touched.
*-----------------------------------------------------------------------------*/
#include "postpos.c"

#include <locale.h>
#ifdef __APPLE__
#include <xlocale.h> /* older SDKs declare newlocale() and uselocale() only here */
#endif
#if !defined(_WIN32) && !defined(LC_NUMERIC_MASK)
/* No feature macro is defined at the top of this file on purpose: -std=gnu99 gives glibc and musl
   POSIX.1-2008 by default, and _GNU_SOURCE would also turn on C23's strtol and scanf, which
   glibc 2.38 and later link as __isoc23_* symbols that older systems do not have */
#error "newlocale(), uselocale() and freelocale() (POSIX.1-2008) are needed, <locale.h> does not declare them"
#endif

#define PPG_MSGLEN 1024

static char ppg_last[PPG_MSGLEN];  /* the last message of the run */
static char ppg_error[PPG_MSGLEN]; /* the last error message of the run */

/* the state of a conversion (ppg_convbin), read by showmsg(): set and read by the thread that holds the lock of
   RTKLIB (convert.rs), so plain variables. ppg_convert_cancelled() is an atomic flag of convert.rs, raised by
   convert_cancel() from any thread */
extern int ppg_convert_cancelled(void);
static int ppg_converting;    /* a conversion runs: showmsg() reads the flag and the counts */
static int ppg_cancel_seen;   /* showmsg() told RTKLIB to stop */
static int ppg_stat[11];      /* the counts of convrnx's last status line, by the letters of PPG_STAT_TYPES */
static char ppg_stat_line[PPG_MSGLEN]; /* that line */
#define PPG_STAT_TYPES "ONGHQLCISET" /* convrnx.c, showstat(): files, then errors (E) and events (T) */

/* a status line of convrnx ("<times>: O=62 N=9 E=1 T=3 ", or "<times>: " before anything is converted): its counts
   into ppg_stat and 1; 0 for any other message */
static int ppg_read_stat(const char *text)
{
    const char *p = strrchr(text, ':');
    int stat[11] = {0};

    if (!p || p[1] != ' ') return 0;
    for (p += 2; *p; ) {
        const char *type;
        char *end;
        long value;

        if (*p == ' ') { p++; continue; }
        if (!(type = strchr(PPG_STAT_TYPES, *p)) || !*type || p[1] != '=') return 0;
        value = strtol(p + 2, &end, 10);
        if (end == p + 2 || (*end && *end != ' ') || value < 0 || value > 2000000000L) return 0;
        stat[type - PPG_STAT_TYPES] = (int)value;
        p = end;
    }
    memcpy(ppg_stat, stat, sizeof(ppg_stat));
    snprintf(ppg_stat_line, sizeof(ppg_stat_line), "%s", text);
    return 1;
}

/* RTKLIB's messages: kept, never printed; 0 = go on (RTKLIB aborts on 1): a
   conversion stops on 1 when convert_cancel() was called --------------------*/
extern int showmsg(const char *format, ...)
{
    char buff[PPG_MSGLEN];
    va_list arg;

    if (!format) return 0;
    va_start(arg, format);
    vsnprintf(buff, sizeof(buff), format, arg);
    va_end(arg);
    /* a conversion keeps its status lines apart, and its progress ("scanning: <time> <systems>") nowhere */
    if (buff[0] && !(ppg_converting && (ppg_read_stat(buff) || !strncmp(buff, "scanning: ", 10)))) {
        snprintf(ppg_last, sizeof(ppg_last), "%s", buff);
        if (!strncmp(buff, "error", 5)) snprintf(ppg_error, sizeof(ppg_error), "%s", buff);
    }
    if (ppg_converting && ppg_convert_cancelled()) {
        ppg_cancel_seen = 1;
        return 1;
    }
    return 0;
}
extern void settspan(gtime_t ts, gtime_t te) { (void)ts; (void)te; }
extern void settime(gtime_t time) { (void)time; }

static void ppg_message(char *msg, int msglen, const char *text)
{
    if (msg && msglen > 0) snprintf(msg, (size_t)msglen, "%s", text);
}

/* The "C" locale of the calling thread --------------------------------------*/
#ifdef _WIN32
/* The C runtime has one locale for the process and, once the thread asks for it, one of its own
   (_configthreadlocale): setlocale() then changes the calling thread alone */
typedef struct {
    int  mode;       /* _configthreadlocale() before: _ENABLE_ or _DISABLE_PER_THREAD_LOCALE */
    char name[256];  /* the numeric locale of the thread before */
} ppg_locale_t;

static int ppg_locale_c(ppg_locale_t *saved)
{
    const char *name;
    size_t len;

    saved->mode = _configthreadlocale(_ENABLE_PER_THREAD_LOCALE);
    if (saved->mode == -1) return 0;
    name = setlocale(LC_NUMERIC, NULL);
    len = name ? strlen(name) : sizeof(saved->name);
    if (len < sizeof(saved->name)) {
        memcpy(saved->name, name, len + 1);
        if (setlocale(LC_NUMERIC, "C")) return 1;
    }
    _configthreadlocale(saved->mode);
    return 0;
}
static void ppg_locale_back(const ppg_locale_t *saved)
{
    setlocale(LC_NUMERIC, saved->name);
    _configthreadlocale(saved->mode);
}
#else
/* newlocale() makes a locale and uselocale() installs it for the calling thread only. All the
   categories of that locale are "C", as in a program that never calls setlocale() (rnx2rtkp) */
typedef struct {
    locale_t c;        /* the new locale */
    locale_t previous; /* the one the thread had: its own, or LC_GLOBAL_LOCALE */
} ppg_locale_t;

static int ppg_locale_c(ppg_locale_t *saved)
{
    saved->c = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0);
    if (!saved->c) return 0;
    saved->previous = uselocale(saved->c);
    if (!saved->previous) { /* (locale_t)0: it could not be installed */
        freelocale(saved->c);
        return 0;
    }
    return 1;
}
static void ppg_locale_back(const ppg_locale_t *saved)
{
    uselocale(saved->previous);
    freelocale(saved->c);
}
#endif

/* RTKLIB decompresses an input with gzip, tar or crx2rnx through system()
   (rtk_uncompress in rtkcmn.c) when the text after the last dot of its path is
   .z .gz .zip .tar .crx or has a 'd' as third character (Hatanaka RINEX 2,
   .25d): a fork of the program and a file written beside the input. Such
   names are refused (any case), as _native.compressed_name does ------------*/
static int ppg_same_nocase(const char *text, const char *lower)
{
    while (*text && *lower && tolower((unsigned char)*text) == *lower) {
        text++;
        lower++;
    }
    return !*text && !*lower;
}
static int ppg_compressed(const char *path)
{
    static const char *exts[] = {".z", ".gz", ".zip", ".tar", ".crx"};
    const char *p = strrchr(path, '.');
    size_t i;

    if (!p) return 0;
    for (i = 0; i < sizeof(exts) / sizeof(*exts); i++) {
        if (ppg_same_nocase(p, exts[i])) return 1;
    }
    return strlen(p) > 3 && (p[3] == 'd' || p[3] == 'D');
}

/* RTKLIB copies a path into a buffer of 1024 bytes without looking at its length: expath() the
   directory of an input (dir[1024]) and the names it finds (buffers of 1024 bytes in rinex.c),
   execses() the solution's name (outfiletm[1024], to which "_events.pos" is added, and the buffers
   of its ".trace" and ".stat" files). A path of 1000 bytes or more is refused, which leaves room
   for what is added: such a path overflows those buffers, on the stack and on the heap ---*/
#define PPG_MAXPATH 1000

static int ppg_too_long(const char *what, const char *path, char *msg, int msglen)
{
    size_t len = strlen(path);
    char text[PPG_MSGLEN];

    if (len < (size_t)PPG_MAXPATH) return 0;
    snprintf(text, sizeof(text), "error : the path of the %s is %d bytes long, and RTKLIB holds paths of "
             "less than %d bytes: use a shorter folder", what, (int)len, PPG_MAXPATH);
    ppg_message(msg, msglen, text);
    return 1;
}

/* the RTKLIB part of ppg_ppk(), from the options to the message: runs in the C locale --*/
static int ppg_run(const char *conf, const char **infiles, int n, const char *outfile,
                   char *msg, int msglen)
{
    prcopt_t prcopt;
    solopt_t solopt;
    filopt_t filopt;
    gtime_t ts = {0}, te = {0};
    int stat;

    /* every option back to RTKLIB's defaults, files too (resetsysopts() keeps
       some of the file options and the strings of the snr mask), then the file */
    memset(&filopt, 0, sizeof(filopt));
    setsysopts(&prcopt_default, &solopt_default, &filopt);
    if (!loadopts(conf, sysopts)) {
        char text[PPG_MSGLEN];
        snprintf(text, sizeof(text), "error : cannot read the options file %s", conf);
        ppg_message(msg, msglen, text);
        return -2;
    }
    getsysopts(&prcopt, &solopt, &filopt);
    if (!prcopt.navsys) prcopt.navsys = SYS_GPS | SYS_GLO | SYS_GAL | SYS_CMP; /* as rnx2rtkp */

    /* postpos.c keeps the invalid time marks of the last run (see above) */
    nitm = iitm = 0;

    stat = postpos(ts, te, 0.0, 0.0, &prcopt, &solopt, &filopt, infiles, n, outfile, "", "");

    ppg_message(msg, msglen, ppg_error[0] ? ppg_error : ppg_last);
    return stat;
}

/* post-processing as rnx2rtkp -k conf -o outfile infiles ----------------------
* args   : char  *conf      I   options file (rnx2rtkp's -k)
*          char **infiles   I   input files: rover obs, base obs, nav, ...
*          int    n         I   number of input files (1 to MAXINFILE)
*          char  *outfile   I   solution file
*          char  *msg       O   last error message, or last message, of RTKLIB
*          int    msglen    I   size of msg
* return : postpos(): 0 done (also when RTKLIB read nothing: see msg),
*          1 aborted, -1 a file named by the options could not be read;
*          -2 the options file could not be read, -3 bad arguments (also a
*          path of 1000 bytes or more: ppg_too_long),
*          -4 a compressed input (ppg_compressed: decompress it first),
*          -5 the calling thread could not be put in the "C" locale for
*          numbers (nothing ran)
* notes  : RTKLIB runs in the "C" locale of the calling thread whatever the
*          locale of the process; the thread's own is back when this returns
*-----------------------------------------------------------------------------*/
int ppg_ppk(const char *conf, const char **infiles, int n, const char *outfile,
            char *msg, int msglen)
{
    ppg_locale_t saved;
    int i, stat;

    ppg_last[0] = ppg_error[0] = '\0';
    ppg_message(msg, msglen, "");

    if (!conf || !*conf || !infiles || n <= 0 || n > MAXINFILE || !outfile || !*outfile) {
        char text[PPG_MSGLEN];
        snprintf(text, sizeof(text), "error : an options file, 1 to %d input files and an output file are needed",
                 MAXINFILE);
        ppg_message(msg, msglen, text);
        return -3;
    }
    if (ppg_too_long("options file", conf, msg, msglen) || ppg_too_long("output file", outfile, msg, msglen)) {
        return -3;
    }
    for (i = 0; i < n; i++) {
        char what[32];

        if (!infiles[i] || !*infiles[i]) {
            ppg_message(msg, msglen, "error : an input file without a name");
            return -3;
        }
        snprintf(what, sizeof(what), "input file %d", i + 1);
        if (ppg_too_long(what, infiles[i], msg, msglen)) return -3;
        if (ppg_compressed(infiles[i])) {
            char text[PPG_MSGLEN];
            snprintf(text, sizeof(text), "error : %s is compressed (gzip, zip, tar or Hatanaka RINEX), "
                     "and RTKLIB would decompress it with a shell command: decompress it first", infiles[i]);
            ppg_message(msg, msglen, text);
            return -4;
        }
    }
    if (!ppg_locale_c(&saved)) {
        ppg_message(msg, msglen, "error : cannot put the thread in the C locale for numbers, "
                    "in which RTKLIB reads and writes them");
        return -5;
    }
    stat = ppg_run(conf, infiles, n, outfile, msg, msglen);
    ppg_locale_back(&saved);
    return stat;
}

/* ------------------------------------------------------------------------------------------------------------------
* The conversion of raw receiver logs into RINEX: what convbin (app/consapp/convbin of RTKLIB-EX) does with
* convrnx() of convrnx.c, for the formats whose decoders this package builds
*-----------------------------------------------------------------------------------------------------------------*/

/* the formats ppg_convbin() knows by name: those with a decoder built here, and those whose decoder is not (their
   entry points are the stubs of rcv_stubs.c: see native/vendor/rtklib/VERSION) */
typedef struct {
    const char *name; /* the name raw_to_rinex() takes */
    int format;       /* STRFMT_ of RTKLIB */
    int built;        /* its decoder is part of this package */
} ppg_format_t;

static const ppg_format_t ppg_formats[] = {
    {"ubx", STRFMT_UBX, 1},         /* u-blox UBX: rcv/ublox.c */
    {"rtcm3", STRFMT_RTCM3, 1},     /* RTCM 3: rtcm.c, rtcm3.c */
    {"novatel", STRFMT_OEM4, 1},    /* NovAtel OEM4/V/6/7, OEMStar: rcv/novatel.c */
    {"sbf", STRFMT_SEPT, 0},        /* Septentrio SBF: rcv/septentrio.c, not built */
    {"rt17", STRFMT_RT17, 0},       /* Trimble RT-17: rcv/rt17.c, not built */
    {"unicore", STRFMT_UNICORE, 0}, /* Unicore: rcv/unicore.c, not built */
};
#define PPG_NFORMATS ((int)(sizeof(ppg_formats) / sizeof(*ppg_formats)))

/* the STRFMT_ code of a format name of ppg_formats (exact spelling), or -1 */
int ppg_convbin_format(const char *name)
{
    int i;

    for (i = 0; name && i < PPG_NFORMATS; i++) {
        if (!strcmp(name, ppg_formats[i].name)) return ppg_formats[i].format;
    }
    return -1;
}

static const ppg_format_t *ppg_format_of(int format)
{
    int i;

    for (i = 0; i < PPG_NFORMATS; i++) {
        if (ppg_formats[i].format == format) return ppg_formats + i;
    }
    return NULL;
}

/* convrnx() expands wild cards (*) in the input path and replaces %-keywords (%Y, %r, ...) in all its paths
   (expath, reppath of rtkcmn.c): a path with either is refused, as one of 1000 bytes or more (ppg_too_long) */
static int ppg_bad_path(const char *what, const char *path, char *msg, int msglen)
{
    char text[PPG_MSGLEN];

    if (!path || !*path) {
        snprintf(text, sizeof(text), "error : the %s has no name", what);
        ppg_message(msg, msglen, text);
        return 1;
    }
    if (ppg_too_long(what, path, msg, msglen)) return 1;
    if (strchr(path, '*') || strchr(path, '%')) {
        snprintf(text, sizeof(text), "error : the path of the %s holds '*' or '%%', which RTKLIB would replace: "
                 "rename it (%s)", what, path);
        ppg_message(msg, msglen, text);
        return 1;
    }
    return 0;
}

#define PPG_NOUTFILE 9 /* the output files of convrnx(): obs, nav, gnav, hnav, qnav, lnav, cnav, inav, sbas */

/* the RTKLIB part of ppg_convbin(): runs in the C locale, the options as convbin sets them ---------------------*/
static int ppg_run_convbin(int format, const char *infile, double approx_gps_seconds, int rinex_version,
                           int navsys, const char *obs_out, const char *nav_out)
{
    rnxopt_t *opt;
    char ofile_[PPG_NOUTFILE][1024] = {{0}}, *ofile[PPG_NOUTFILE];
    int i, j, stat;

    if (!(opt = (rnxopt_t *)calloc(1, sizeof(rnxopt_t)))) {
        showmsg("error : out of memory");
        return 0;
    }
    /* convbin's defaults (cmdopts of convbin.c): observations of code and carrier phase, every frequency (-f 6), a
       tolerance of 5 ms between epochs, no signal masked */
    opt->rnxver = rinex_version;
    opt->obstype = OBSTYPE_PR | OBSTYPE_CP;
    opt->navsys = navsys;
    opt->ttol = 0.005;
    for (i = 0; i < RNX_NUMSYS; i++) {
        for (j = 0; j < MAXCODE; j++) opt->mask[i][j] = '1';
        opt->mask[i][MAXCODE] = '\0';
    }
    opt->freqtype = FREQTYPE_L1 | FREQTYPE_L2 | FREQTYPE_L3 | FREQTYPE_L4 | FREQTYPE_L5 | FREQTYPE_L6;
    if (approx_gps_seconds > 0.0) {
        /* gpst2time() takes the seconds of a week (it drops more than 1e9 s) */
        int week = (int)floor(approx_gps_seconds / 604800.0);
        opt->trtcm = gpst2time(week, approx_gps_seconds - week * 604800.0);
    }
    snprintf(opt->prog, sizeof(opt->prog), "%s %s %s", "PPG_CORE", VER_RTKLIB, PATCH_LEVEL);

    for (i = 0; i < PPG_NOUTFILE; i++) ofile[i] = ofile_[i];
    snprintf(ofile[0], 1024, "%s", obs_out);
    snprintf(ofile[1], 1024, "%s", nav_out);

    stat = convrnx(format, opt, infile, ofile);
    free(opt);
    return stat;
}

/* a raw receiver log converted into RINEX observations and navigation, as convbin -r <format> -v <version>
*  [-tr <approximate time>] -o obs_out -n nav_out infile
* args   : int    format             I   STRFMT_UBX, STRFMT_RTCM3 or STRFMT_OEM4 (ppg_convbin_format())
*          char  *infile             I   the log
*          double approx_gps_seconds I   an approximate time of the log, seconds since 1980-01-06 GPS time (needed
*                                        by RTCM 3, which carries the time of the week only); 0 = none
*          int    rinex_version      I   RINEX version x100, 300 to 305 (one navigation file for all the systems)
*          int    navsys             I   the systems converted, SYS_ mask
*          char  *obs_out, *nav_out  I   the RINEX observation and navigation files written
*          int   *counts             O   counts[4]: epochs, events, navigation messages, errors (convrnx's n[])
*          char  *msg                O   RTKLIB's last message, or the error
*          int    msglen             I   size of msg
* return : 0 done, 1 cancelled (convert_cancel), -1 the log could not be read or an output could not be opened
*          for writing, -2 nothing decoded (not a log of
*          that format, or damaged; RTKLIB's message), -3 bad arguments (a path of 1000 bytes or more or with '*' or
*          '%', a format not known, no approximate time for RTCM 3, a version or systems out of range), -5 the
*          thread could not be put in the "C" locale for numbers, -6 the format is known but its decoder is not
*          part of this package
* notes  : not reentrant (RTKLIB's globals): the caller (convert.rs) holds the lock shared with ppg_ppk(). The
*          outputs may be left behind, whole or not, on any code but 0: the caller writes to temporary names and
*          removes them. RINEX is never an input (convrnx would decompress it through a shell command).
*-----------------------------------------------------------------------------------------------------------------*/
int ppg_convbin(int format, const char *infile, double approx_gps_seconds, int rinex_version, int navsys,
                const char *obs_out, const char *nav_out, int *counts, char *msg, int msglen)
{
    const ppg_format_t *known = ppg_format_of(format);
    ppg_locale_t saved;
    char text[PPG_MSGLEN];
    FILE *fp;
    int i, stat;

    ppg_last[0] = ppg_error[0] = '\0';
    ppg_message(msg, msglen, "");
    for (i = 0; counts && i < 4; i++) counts[i] = 0;

    if (!counts) {
        ppg_message(msg, msglen, "error : no room for the counts");
        return -3;
    }
    if (!known) {
        snprintf(text, sizeof(text), "error : the format %d is not one of the raw formats converted here", format);
        ppg_message(msg, msglen, text);
        return -3;
    }
    if (ppg_bad_path("input file", infile, msg, msglen) || ppg_bad_path("observation file", obs_out, msg, msglen) ||
        ppg_bad_path("navigation file", nav_out, msg, msglen)) {
        return -3;
    }
    if (!strcmp(obs_out, nav_out) || !strcmp(obs_out, infile) || !strcmp(nav_out, infile)) {
        ppg_message(msg, msglen, "error : the input, observation and navigation files must be three files");
        return -3;
    }
    if (rinex_version < 300 || rinex_version > 305) {
        snprintf(text, sizeof(text), "error : RINEX version %d.%02d: 3.00 to 3.05 are written here",
                 rinex_version / 100, rinex_version % 100);
        ppg_message(msg, msglen, text);
        return -3;
    }
    if (!(navsys & (SYS_GPS | SYS_GLO | SYS_GAL | SYS_QZS | SYS_SBS | SYS_CMP | SYS_IRN))) {
        ppg_message(msg, msglen, "error : no satellite system to convert");
        return -3;
    }
    if (!known->built) {
        snprintf(text, sizeof(text), "error : the decoder of the %s format is not built into this package (its "
                 "licence is not checked: see vendor/rtklib/VERSION of the native core); convert the log with "
                 "convbin of RTKLIB or the receiver's software", known->name);
        ppg_message(msg, msglen, text);
        return -6;
    }
    if ((format == STRFMT_RTCM3 || format == STRFMT_RT17) && !(approx_gps_seconds > 0.0)) {
        ppg_message(msg, msglen, "error : RTCM 3 carries the time of the week only: an approximate time of the log "
                    "(its day) is needed");
        return -3;
    }
    if (!(fp = fopen(infile, "rb"))) {
        snprintf(text, sizeof(text), "error : cannot read %s", infile);
        ppg_message(msg, msglen, text);
        return -1;
    }
    fclose(fp);

    if (!ppg_locale_c(&saved)) {
        ppg_message(msg, msglen, "error : cannot put the thread in the C locale for numbers, "
                    "in which RTKLIB reads and writes them");
        return -5;
    }
    memset(ppg_stat, 0, sizeof(ppg_stat));
    ppg_stat_line[0] = '\0';
    ppg_cancel_seen = 0;
    ppg_converting = 1;
    stat = ppg_run_convbin(format, infile, approx_gps_seconds, rinex_version, navsys, obs_out, nav_out);
    ppg_converting = 0;
    ppg_locale_back(&saved);

    counts[0] = ppg_stat[0];                                     /* O: observation epochs */
    counts[1] = ppg_stat[10];                                    /* T: events */
    for (i = 1; i <= 7; i++) counts[2] += ppg_stat[i];           /* N G H Q L C I: navigation messages */
    counts[3] = ppg_stat[9];                                     /* E: errors */

    if (ppg_cancel_seen) {
        ppg_message(msg, msglen, "cancelled");
        return 1;
    }
    if (stat <= 0 && !strncmp(ppg_last, "file open error: ", 17)) { /* openfile() of convrnx.c */
        snprintf(text, sizeof(text), "error : cannot write %s (a folder missing or not writable)", ppg_last + 17);
        ppg_message(msg, msglen, text);
        return -1;
    }
    if (stat <= 0 || counts[0] <= 0) {
        snprintf(text, sizeof(text), "error : no observations decoded from %s: not a log of the %s format, or damaged%s%s",
                 infile, known->name, ppg_last[0] ? " (RTKLIB: " : "", ppg_last[0] ? ppg_last : "");
        if (ppg_last[0]) strncat(text, ")", sizeof(text) - strlen(text) - 1);
        ppg_message(msg, msglen, text);
        return -2;
    }
    ppg_message(msg, msglen, ppg_stat_line);
    return 0;
}
