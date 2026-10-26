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
*          -2 the options file could not be read, -3 bad arguments,
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
    if (!ppg_locale_c(&saved)) {
        ppg_message(msg, msglen, "error : cannot put the thread in the C locale for numbers, "
                    "in which RTKLIB reads and writes them");
        return -5;
    }
    stat = ppg_run(conf, infiles, n, outfile, msg, msglen);
    ppg_locale_back(&saved);
    return stat;
}
