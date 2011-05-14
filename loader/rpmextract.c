/* unpack the payload of RPM package to the current directory
 * 
 * File name: rpmextract.c
 * Date:      2009/12/18
 * Author:    Martin Sivak <msivak at redhat dot com>
 * 
 * Copyright (C) 2009 Red Hat, Inc. All rights reserved.
 * 
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License as
 * published by the Free Software Foundation; either version 2 of the
 * License, or (at your option) any later version.
 * 
 * This program is distributed in the hope that it will be useful, but
 * WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program. If not, see <http://www.gnu.org/licenses/>.
 *
 * */

#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#include <rpm/rpmlib.h>		/* rpmReadPackageFile .. */
#include <rpm/rpmtag.h>
#include <rpm/rpmio.h>
#include <rpm/rpmpgp.h>

#include <rpm/rpmts.h>

#include <stdio.h>
#include <archive.h>
#include <archive_entry.h>

#include "loader.h"
#include "rpmextract.h"
#include "unpack.h"

#include "../pyanaconda/isys/log.h"

/*
 * internal structure to pass to libarchive callbacks
 */

struct cpio_mydata {
    FD_t gzdi;
    char *buffer;
};

/*
 * libarchive callbacks
 */

ssize_t rpm_myread(struct archive *a, void *client_data, const void **buff)
{
    struct cpio_mydata *mydata = client_data;
    *buff = mydata->buffer;
    return Fread(mydata->buffer, 1, BUFFERSIZE, mydata->gzdi);
}

int rpm_myclose(struct archive *a, void *client_data)
{
    struct cpio_mydata *mydata = client_data;
    if (mydata->gzdi > 0)
        Fclose(mydata->gzdi);
    return ARCHIVE_OK;
}

/* read data from RPM header */

const char * headerGetString(Header h, rpmTag tag)
{
    const char *res = NULL;
    struct rpmtd_s td;

    if (headerGet(h, tag, &td, HEADERGET_MINMEM)) {
        if (rpmtdCount(&td) == 1) {
            res = rpmtdGetString(&td);
        }
        rpmtdFreeData(&td);
    }
    return res;
}

/*
 * explode source RPM into the current directory
 * use filters to skip packages and files we do not need
 */
int explodeRPM(const char *source,
        filterfunc filter,
        dependencyfunc provides,
        dependencyfunc deps,
        void* userptr,
        char *destination)
{
    char buffer[BUFFERSIZE+1]; /* make space for trailing \0 */
    FD_t fdi;
    Header h;
    char * rpmio_flags = NULL;
    rpmRC rc;
    FD_t gzdi;
    struct archive *cpio;
    struct cpio_mydata cpio_mydata;

    rpmts ts;
    rpmVSFlags vsflags;
    const char *compr;

    if (strcmp(source, "-") == 0)
        fdi = fdDup(STDIN_FILENO);
    else
        fdi = Fopen(source, "r.ufdio");

    if (Ferror(fdi)) {
        const char *srcname = (strcmp(source, "-") == 0) ? "<stdin>" : source;
        logMessage(ERROR, "%s: %s\n", srcname, Fstrerror(fdi));
        return EXIT_FAILURE;
    }
    rpmReadConfigFiles(NULL, NULL);

    /* Initialize RPM transaction */
    ts = rpmtsCreate();
    vsflags = 0;

    /* Do not check digests, signatures or headers */
    vsflags |= _RPMVSF_NODIGESTS;
    vsflags |= _RPMVSF_NOSIGNATURES;
    vsflags |= RPMVSF_NOHDRCHK;
    (void) rpmtsSetVSFlags(ts, vsflags);

    rc = rpmReadPackageFile(ts, fdi, "rpm2dir", &h);

    ts = rpmtsFree(ts);

    switch (rc) {
        case RPMRC_OK:
        case RPMRC_NOKEY:
        case RPMRC_NOTTRUSTED:
            break;
        case RPMRC_NOTFOUND:
            logMessage(ERROR, "%s is not an RPM package", source);
            return EXIT_FAILURE;
            break;
        case RPMRC_FAIL:
        default:
            logMessage(ERROR, "error reading header from %s package\n", source);
            return EXIT_FAILURE;
            break;
    }

    /* Retrieve all dependencies and run them through deps function */
    while (deps) {
        struct rpmtd_s tddep;
        struct rpmtd_s tdver;
        const char *depname;
        const char *depversion;

        if (!headerGet(h, RPMTAG_PROVIDES, &tddep, HEADERGET_MINMEM))
            break;

        if (!headerGet(h, RPMTAG_PROVIDEVERSION, &tdver, HEADERGET_MINMEM)){
            rpmtdFreeData(&tddep);
            break;
        }

        /* iterator */
        while ((depname = rpmtdNextString(&tddep))) {
            depversion = rpmtdNextString(&tdver);
            if (deps(depname, depversion, userptr)) {
                rpmtdFreeData(&tddep);
                rpmtdFreeData(&tdver);
                Fclose(fdi);
                return EXIT_BADDEPS;
            }
        }

        rpmtdFreeData(&tddep);
        rpmtdFreeData(&tdver);

        break;
    }

    /* Retrieve all provides and run them through provides function */
    while (provides) {
        struct rpmtd_s tddep;
        struct rpmtd_s tdver;
        const char *depname;
        const char *depversion;
        int found = 0;

        if (!headerGet(h, RPMTAG_PROVIDES, &tddep, HEADERGET_MINMEM))
            break;

        if (!headerGet(h, RPMTAG_PROVIDEVERSION, &tdver, HEADERGET_MINMEM)){
            rpmtdFreeData(&tddep);
            break;
        }

        /* iterator */
        while ((depname = rpmtdNextString(&tddep))) {
            depversion = rpmtdNextString(&tdver);
            if (!provides(depname, depversion, userptr)) {
                found++;
            }
        }

        rpmtdFreeData(&tddep);
        rpmtdFreeData(&tdver);

        if (found<=0){
            Fclose(fdi);
            return EXIT_BADDEPS;
        }
        break;
    }

    /* Retrieve type of payload compression. */
    compr = headerGetString(h, RPMTAG_PAYLOADCOMPRESSOR);
    if (compr && strcmp(compr, "gzip")) {
        checked_asprintf(&rpmio_flags, "r.%sdio", compr);
    }
    else {
        checked_asprintf(&rpmio_flags, "r.gzdio");
    }

    /* Open uncompressed cpio stream */
    gzdi = Fdopen(fdi, rpmio_flags);
    free(rpmio_flags);

    if (gzdi == NULL) {
        logMessage(ERROR, "cannot re-open payload: %s", Fstrerror(gzdi));
        return EXIT_FAILURE;
    }

    cpio_mydata.gzdi = gzdi;
    cpio_mydata.buffer = buffer;

    /* initialize cpio decompressor */
    if (unpack_init(&cpio) != ARCHIVE_OK) {
        Fclose(gzdi);
        return -1;
    }

    rc = archive_read_open(cpio, &cpio_mydata, NULL, rpm_myread, rpm_myclose);

    /* check the status of archive_open */
    if (rc != ARCHIVE_OK){
        Fclose(gzdi);
        return -1;
    }

    /* read all files in cpio archive and close */
    rc = unpack_members_and_finish(cpio, destination, filter, userptr);

    return rc != ARCHIVE_OK;
}
