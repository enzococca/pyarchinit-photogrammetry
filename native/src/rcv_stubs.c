/*------------------------------------------------------------------------------
* rcv_stubs.c : the receiver decoders of RTKLIB that ppg_core does not build
*
* rcvraw.c (RTKLIB-EX, unchanged) calls the decoder of every receiver format
* RTKLIB knows. ppg_core builds two of them from RTKLIB's src/rcv: ublox.c and
* novatel.c (T. Takasu, under RTKLIB's BSD 2-clause licence); RTCM 3 is decoded
* by rtcm*.c, which the post-processing already builds. The other decoders of
* src/rcv are files of other authors whose headers carry no licence of their
* own (see native/vendor/rtklib/VERSION) and are not part of this package.
*
* These are their entry points, written here: they decode nothing and do not
* touch the raw_t they are given. A stream decoder answers 0 (no message yet),
* a file decoder -2 (end of the file), an initialisation 0 (failed), so that
* nothing is ever read through them; ppg_convbin() refuses their formats before
* RTKLIB is called (ppk_shim.c, code -6).
*-----------------------------------------------------------------------------*/
#include "rtklib.h"

#define PPG_NOT_BUILT(name)                                                    \
    extern int input_##name(raw_t *raw, uint8_t data)                         \
    {                                                                          \
        (void)raw;                                                             \
        (void)data;                                                            \
        return 0;                                                              \
    }                                                                          \
    extern int input_##name##f(raw_t *raw, FILE *fp)                          \
    {                                                                          \
        (void)raw;                                                             \
        (void)fp;                                                              \
        return -2;                                                             \
    }

PPG_NOT_BUILT(sbp)     /* Swift Navigation SBP (swiftnav.c) */
PPG_NOT_BUILT(cres)    /* Hemisphere Crescent (crescent.c) */
PPG_NOT_BUILT(stq)     /* SkyTraq (skytraq.c) */
PPG_NOT_BUILT(javad)   /* Javad GREIS (javad.c) */
PPG_NOT_BUILT(nvs)     /* NVS BINR (nvs.c) */
PPG_NOT_BUILT(bnx)     /* BINEX (binex.c) */
PPG_NOT_BUILT(rt17)    /* Trimble RT-17 (rt17.c) */
PPG_NOT_BUILT(sbf)     /* Septentrio SBF (septentrio.c) */
PPG_NOT_BUILT(unicore) /* Unicore (unicore.c) */

extern int init_rt17(raw_t *raw)
{
    (void)raw;
    return 0;
}
extern void free_rt17(raw_t *raw)
{
    (void)raw;
}
extern int init_sbf(raw_t *raw)
{
    (void)raw;
    return 0;
}
extern void free_sbf(raw_t *raw)
{
    (void)raw;
}
