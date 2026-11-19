/* Directory RPCs with names no libc call sends, for the overlayfs test.
   overlayfs_raw OP DIR ARGS... sends one and prints "OP: ERROR".

     mkdir DIR NAME             rmdir DIR NAME          unlink DIR NAME
     rename DIR NAME TODIR TONAME
     link DIR FILE NAME         link FILE (looked up) as NAME in DIR
     symlink DIR TARGET NAME    as glibc makes a symlink: dir_mkfile, the
                                symlink translator record, dir_link
     mkfile-link DIR NAME       dir_mkfile, a write, dir_link as NAME  */
#define _GNU_SOURCE
#include <hurd.h>
#include <hurd/fs.h>
#include <hurd/io.h>
#include <hurd/paths.h>
#include <errno.h>
#include <error.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>

static file_t
op (const char *path, int flags)
{
  file_t f = file_name_lookup (path, flags, 0);
  if (f == MACH_PORT_NULL)
    error (2, errno, "open %s", path);
  return f;
}

int
main (int argc, char **argv)
{
  error_t err = EINVAL;
  const char *o = argc > 3 ? argv[1] : "";
  file_t f = MACH_PORT_NULL;

  if (!strcmp (o, "mkdir"))
    err = dir_mkdir (op (argv[2], 0), argv[3], 0755);
  else if (!strcmp (o, "rmdir"))
    err = dir_rmdir (op (argv[2], 0), argv[3]);
  else if (!strcmp (o, "unlink"))
    err = dir_unlink (op (argv[2], 0), argv[3]);
  else if (!strcmp (o, "rename") && argc == 6)
    err = dir_rename (op (argv[2], 0), argv[3], op (argv[4], 0), argv[5], 0);
  else if (!strcmp (o, "link") && argc == 5)
    err = dir_link (op (argv[2], 0), op (argv[3], O_NOLINK | O_NOTRANS), argv[4], 0);
  else if (!strcmp (o, "symlink") && argc == 5)
    {
      char buf[1024];
      size_t n = sizeof _HURD_SYMLINK;
      memcpy (buf, _HURD_SYMLINK, n);
      strncpy (buf + n, argv[3], sizeof buf - n - 1);
      buf[sizeof buf - 1] = '\0';
      n += strlen (buf + n) + 1;
      err = dir_mkfile (op (argv[2], 0), O_WRITE, 0777, &f);
      if (!err)
	err = file_set_translator (f, FS_TRANS_EXCL | FS_TRANS_SET, 0, 0, buf, n,
				   MACH_PORT_NULL, MACH_MSG_TYPE_COPY_SEND);
      if (!err)
	err = dir_link (op (argv[2], 0), f, argv[4], 1);
    }
  else if (!strcmp (o, "mkfile-link"))
    {
      vm_size_t n;
      err = dir_mkfile (op (argv[2], 0), O_WRITE, 0644, &f);
      if (!err)
	err = io_write (f, "payload\n", 8, -1, &n);
      if (!err)
	err = dir_link (op (argv[2], 0), f, argv[3], 1);
    }
  else
    {
      fprintf (stderr, "usage: see the head of overlayfs_raw.c\n");
      return 2;
    }
  printf ("%s: %s\n", o, err ? strerror (err) : "ok");
  return err != 0;
}
