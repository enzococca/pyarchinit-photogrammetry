/*------------------------------------------------------------------------------
* ppk_shim.c : RTKLIB's post-processing (rnx2rtkp -k) as one function of ppg_core
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
*-----------------------------------------------------------------------------*/
#include "postpos.c"

#define PPG_MSGLEN 1024

static char ppg_last[PPG_MSGLEN];  /* the last message of the run */
static char ppg_error[PPG_MSGLEN]; /* the last error message of the run */

/* RTKLIB's messages: kept, never printed; 0 = go on (RTKLIB aborts on 1) ------*/
extern int showmsg(const char *format, ...)
{
    char buff[PPG_MSGLEN];
    va_list arg;

    if (!format) return 0;
    va_start(arg, format);
    vsnprintf(buff, sizeof(buff), format, arg);
    va_end(arg);
    if (!buff[0]) return 0;
    snprintf(ppg_last, sizeof(ppg_last), "%s", buff);
    if (!strncmp(buff, "error", 5)) snprintf(ppg_error, sizeof(ppg_error), "%s", buff);
    return 0;
}
extern void settspan(gtime_t ts, gtime_t te) { (void)ts; (void)te; }
extern void settime(gtime_t time) { (void)time; }

static void ppg_message(char *msg, int msglen, const char *text)
{
    if (msg && msglen > 0) snprintf(msg, (size_t)msglen, "%s", text);
}

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

/* post-processing as rnx2rtkp -k conf -o outfile infiles ----------------------
* args   : char  *conf      I   options file (rnx2rtkp's -k)
*          char **infiles   I   input files: rover obs, base obs, nav, ...
*          int    n         I   number of input files (1 to MAXINFILE)
*          char  *outfile   I   solution file
*          char  *msg       O   last error message, or last message, of RTKLIB
*          int    msglen    I   size of msg
* return : postpos(): 0 done (also when RTKLIB read nothing: see msg),
*          1 aborted, -1 a file named by the options could not be read;
*          -2 the options file could not be read, -3 bad arguments,
*          -4 a compressed input (ppg_compressed: decompress it first)
*-----------------------------------------------------------------------------*/
int ppg_ppk(const char *conf, const char **infiles, int n, const char *outfile,
            char *msg, int msglen)
{
    prcopt_t prcopt;
    solopt_t solopt;
    filopt_t filopt;
    gtime_t ts = {0}, te = {0};
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
    for (i = 0; i < n; i++) {
        if (!infiles[i] || !*infiles[i]) {
            ppg_message(msg, msglen, "error : an input file without a name");
            return -3;
        }
        if (ppg_compressed(infiles[i])) {
            char text[PPG_MSGLEN];
            snprintf(text, sizeof(text), "error : %s is compressed (gzip, zip, tar or Hatanaka RINEX), "
                     "and RTKLIB would decompress it with a shell command: decompress it first", infiles[i]);
            ppg_message(msg, msglen, text);
            return -4;
        }
    }
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
