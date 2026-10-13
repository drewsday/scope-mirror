# 32-bit VISA bridge for scope_mirror.py.
#
# Run with the 32-bit Windows PowerShell (SysWOW64) so it loads the 32-bit
# visa32.dll. This is used when the 64-bit VISA stack is unusable.
#
# Line protocol on stdin/stdout (ASCII):
#   in : W|<scpi>        write a command            -> OK|
#        Q|<scpi>        write, then read response  -> OK|<base64 of raw response>
#        X|              quit
#   out: READY|<resource>  once connected,  ERR|<message> on any failure
param([string]$Resource = "")

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class Bridge {
    const string DLL = "visa32.dll";
    [DllImport(DLL)] static extern int viOpenDefaultRM(out uint rm);
    [DllImport(DLL)] static extern int viFindRsrc(uint rm, string expr, out uint list, out uint n, StringBuilder desc);
    [DllImport(DLL)] static extern int viFindNext(uint list, StringBuilder desc);
    [DllImport(DLL)] static extern int viOpen(uint rm, string name, uint mode, uint tmo, out uint vi);
    [DllImport(DLL)] static extern int viClose(uint obj);
    [DllImport(DLL)] static extern int viRead(uint vi, byte[] buf, uint cnt, out uint ret);
    [DllImport(DLL)] static extern int viWrite(uint vi, byte[] buf, uint cnt, out uint ret);
    [DllImport(DLL)] static extern int viSetAttribute(uint vi, uint attr, uint val);

    const int VI_SUCCESS_MAX_CNT = 0x3FFF0006;
    const uint VI_ATTR_TMO_VALUE = 0x3FFF001A;

    static void Check(int status, string what) {
        if (status < 0) throw new Exception(what + " failed: 0x" + status.ToString("X8"));
    }

    static void Write(uint vi, string cmd) {
        byte[] b = Encoding.ASCII.GetBytes(cmd + "\n");
        uint ret;
        Check(viWrite(vi, b, (uint)b.Length, out ret), "viWrite");
    }

    static byte[] ReadAll(uint vi) {
        var ms = new MemoryStream();
        byte[] buf = new byte[65536];
        while (true) {
            uint ret;
            int st = viRead(vi, buf, (uint)buf.Length, out ret);
            if (st < 0) throw new Exception("viRead failed: 0x" + st.ToString("X8"));
            ms.Write(buf, 0, (int)ret);
            if (st != VI_SUCCESS_MAX_CNT) break;
        }
        return ms.ToArray();
    }

    static string FindScope(uint rm) {
        uint list, n;
        var desc = new StringBuilder(256);
        if (viFindRsrc(rm, "USB?*INSTR", out list, out n, desc) < 0 || n == 0)
            throw new Exception("no USB instruments found");
        var all = new List<string>();
        all.Add(desc.ToString());
        for (uint i = 1; i < n; i++) {
            desc.Length = 0;
            if (viFindNext(list, desc) < 0) break;
            all.Add(desc.ToString());
        }
        viClose(list);
        foreach (var r in all) if (r.ToLower().Contains("0x0699")) return r;
        return all[0];
    }

    public static void Run(string resource) {
        var stdin = new StreamReader(Console.OpenStandardInput(), Encoding.ASCII);
        var stdout = new StreamWriter(Console.OpenStandardOutput(), Encoding.ASCII);
        stdout.AutoFlush = true;
        stdout.NewLine = "\n";
        uint rm = 0, vi = 0;
        try {
            Check(viOpenDefaultRM(out rm), "viOpenDefaultRM");
            if (String.IsNullOrEmpty(resource)) resource = FindScope(rm);
            Check(viOpen(rm, resource, 0, 5000, out vi), "viOpen " + resource);
            viSetAttribute(vi, VI_ATTR_TMO_VALUE, 5000);
        } catch (Exception e) {
            stdout.WriteLine("ERR|" + e.Message);
            return;
        }
        stdout.WriteLine("READY|" + resource);
        string line;
        while ((line = stdin.ReadLine()) != null) {
            try {
                int bar = line.IndexOf('|');
                string op = line.Substring(0, bar), arg = line.Substring(bar + 1);
                if (op == "W") { Write(vi, arg); stdout.WriteLine("OK|"); }
                else if (op == "Q") { Write(vi, arg); stdout.WriteLine("OK|" + Convert.ToBase64String(ReadAll(vi))); }
                else if (op == "R") { stdout.WriteLine("OK|" + Convert.ToBase64String(ReadAll(vi))); }
                else if (op == "X") break;
            } catch (Exception e) {
                stdout.WriteLine("ERR|" + e.Message.Replace('\n', ' ').Replace('\r', ' '));
            }
        }
        viClose(vi);
        viClose(rm);
    }
}
'@

[Bridge]::Run($Resource)
