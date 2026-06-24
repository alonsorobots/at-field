// AT-Field headless sensor helper.
//
// Reads hardware sensors directly through LibreHardwareMonitorLib (the
// same library LHM's GUI uses) and streams them to stdout as one compact
// JSON object per line (JSONL). This deliberately AVOIDS LHM's optional
// GUI web server -- that path (HttpListener on http://+:port/, driven by
// a background service) proved fragile: it depends on URL ACLs, a kernel
// driver loading inside a Session-0 GUI process, and breaks on recent
// Windows http.sys updates. Reading the library in-process removes every
// one of those failure modes.
//
// Target framework: .NET Framework 4.7.2 (LibreHardwareMonitorLib calls
// Framework-only APIs such as Mutex(..., MutexSecurity)). Language level
// is kept at C# 5 so the in-box compiler (no .NET SDK required) can build
// it:  %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
//
// Driver-backed sensors (CPU package temp via MSR) require the process to
// be elevated; the AT-Field watchdog runs as LocalSystem, so it gets
// them. GPU memory-junction temp needs no driver/elevation.
//
// Protocol (stdout, one line each):
//   * On start:   {"event":"ready","ts":...,"elevated":bool}
//   * Each tick:  {"event":"sample","ts":...,"sensors":[{...}, ...]}
//   * On error:   {"event":"error","ts":...,"message":"..."}
// The parent (AT-Field) reads the latest "sample" line. Closing stdin or
// terminating the process stops the helper; it releases the driver on exit.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Principal;
using System.Threading;
using LibreHardwareMonitor.Hardware;
using Newtonsoft.Json;

namespace AtField
{
    internal static class Program
    {
        // Sensor types worth streaming. Keeps the payload small and stable.
        private static readonly HashSet<SensorType> Wanted = new HashSet<SensorType>
        {
            SensorType.Temperature,
            SensorType.Voltage,
            SensorType.Power,
        };

        private static volatile bool _running = true;

        private static int Main(string[] args)
        {
            double intervalSec = 1.0;
            bool once = false;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--interval" && i + 1 < args.Length &&
                    double.TryParse(args[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out intervalSec))
                {
                    if (intervalSec < 0.1) intervalSec = 0.1;
                    i++;
                }
                else if (args[i] == "--once")
                {
                    once = true;
                }
            }

            Console.CancelKeyPress += delegate(object s, ConsoleCancelEventArgs e) { _running = false; e.Cancel = true; };

            Computer computer = new Computer
            {
                IsCpuEnabled = true,
                IsGpuEnabled = true,
                IsMotherboardEnabled = true,
                IsControllerEnabled = true,
                IsMemoryEnabled = false,
                IsStorageEnabled = false,
                IsNetworkEnabled = false,
            };

            try
            {
                computer.Open();
            }
            catch (Exception ex)
            {
                EmitError("Computer.Open() failed: " + ex.Message);
                return 2;
            }

            Emit(Obj("event", "ready", "ts", UnixNow(), "elevated", IsElevated()));

            // Watch stdin on a background thread: when the parent closes the
            // pipe (or dies), Read() returns -1 and we shut down cleanly.
            Thread stdinWatch = new Thread(delegate()
            {
                try { while (Console.In.Read() != -1) { } }
                catch { }
                _running = false;
            });
            stdinWatch.IsBackground = true;
            stdinWatch.Start();

            int sleepMs = (int)(intervalSec * 1000);
            try
            {
                do
                {
                    try
                    {
                        EmitSample(computer);
                    }
                    catch (Exception ex)
                    {
                        EmitError("sample failed: " + ex.Message);
                    }

                    if (once) break;

                    int slept = 0;
                    while (_running && slept < sleepMs)
                    {
                        int slice = sleepMs - slept;
                        if (slice > 100) slice = 100;
                        Thread.Sleep(slice);
                        slept += slice;
                    }
                } while (_running);
            }
            finally
            {
                try { computer.Close(); }
                catch { }
            }

            return 0;
        }

        private static void EmitSample(Computer computer)
        {
            List<object> sensors = new List<object>();
            foreach (IHardware hw in computer.Hardware)
            {
                CollectHardware(hw, sensors);
            }
            Emit(Obj("event", "sample", "ts", UnixNow(), "sensors", sensors));
        }

        private static void CollectHardware(IHardware hw, List<object> sink)
        {
            hw.Update();
            foreach (ISensor s in hw.Sensors)
            {
                if (!Wanted.Contains(s.SensorType)) continue;

                object value = null;
                if (s.Value.HasValue) value = Math.Round(s.Value.Value, 3);

                string id = s.Identifier == null ? null : s.Identifier.ToString();
                string hwId = hw.Identifier == null ? null : hw.Identifier.ToString();

                sink.Add(Obj(
                    "id", id,
                    "hw", hw.Name,
                    "hwId", hwId,
                    "hwType", hw.HardwareType.ToString(),
                    "name", s.Name,
                    "type", s.SensorType.ToString(),
                    "value", value));
            }
            foreach (IHardware sub in hw.SubHardware)
            {
                CollectHardware(sub, sink);
            }
        }

        // Builds an ordered dictionary from alternating key/value args.
        // (Kept instead of C# 6 index initializers so the in-box C# 5
        // compiler can build this file.)
        private static Dictionary<string, object> Obj(params object[] kv)
        {
            Dictionary<string, object> d = new Dictionary<string, object>(kv.Length / 2);
            for (int i = 0; i + 1 < kv.Length; i += 2)
            {
                d[(string)kv[i]] = kv[i + 1];
            }
            return d;
        }

        private static void Emit(object obj)
        {
            Console.Out.WriteLine(JsonConvert.SerializeObject(obj));
            Console.Out.Flush();
        }

        private static void EmitError(string message)
        {
            Emit(Obj("event", "error", "ts", UnixNow(), "message", message));
        }

        private static double UnixNow()
        {
            return (DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
        }

        private static bool IsElevated()
        {
            try
            {
                using (WindowsIdentity id = WindowsIdentity.GetCurrent())
                {
                    return new WindowsPrincipal(id).IsInRole(WindowsBuiltInRole.Administrator);
                }
            }
            catch { return false; }
        }
    }
}
