using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace DesktopHost
{
    internal static class AppSpec
    {
        public static readonly string AppSlug = "web-crawler";
        public static readonly string WindowTitle = "Web Crawler 控制台";
        public static readonly string Subtitle = "正在以默认安全模式启动本地采集控制台，关闭窗口将自动停止服务…";
        public static readonly string MarkerFile = "pyproject.toml";
        public static readonly string HealthPath = "/";
        public static readonly int[] PreferredPorts = new int[] { 18765, 18766, 18767 };
        public static readonly int StartupTimeoutSeconds = 35;
        public static readonly bool AcceptStatus503 = false;
        public static readonly bool LoadDotEnv = true;
        public static readonly bool RequireDotEnv = false;
        public static readonly bool StripPowerModeByDefault = true;
        public static readonly string ThemeBgHex = "#0F172A";
        public static readonly string AccentHex = "#0F766E";

        private static string FindExecutableInPath(string exeName)
        {
            string pathEnv = Environment.GetEnvironmentVariable("PATH") ?? "";
            foreach (string dir in pathEnv.Split(';'))
            {
                if (string.IsNullOrWhiteSpace(dir)) continue;
                try
                {
                    string candidate = Path.Combine(dir.Trim(), exeName);
                    if (File.Exists(candidate)) return candidate;
                }
                catch { }
            }
            return null;
        }

        private static string FindPython(string projectRoot, out string extraPrefix)
        {
            extraPrefix = "";
            string venvPy = Path.Combine(projectRoot, ".venv", "Scripts", "python.exe");
            if (File.Exists(venvPy)) return venvPy;
            string pyLauncher = FindExecutableInPath("py.exe");
            if (!string.IsNullOrEmpty(pyLauncher))
            {
                extraPrefix = "-3 ";
                return pyLauncher;
            }
            string sysPy = FindExecutableInPath("python.exe");
            if (!string.IsNullOrEmpty(sysPy) && sysPy.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) < 0)
            {
                return sysPy;
            }
            return null;
        }

        public static ProcessStartInfo BuildServerStartInfo(
            string projectRoot,
            int port,
            bool powerMode,
            out string setupError,
            out string setupHint)
        {
            setupError = null;
            setupHint = null;
            string prefix;
            string python = FindPython(projectRoot, out prefix);
            if (string.IsNullOrEmpty(python))
            {
                setupError = "未找到可用的 Python 解释器";
                setupHint = "请安装 Python 3.10+ 或创建项目虚拟环境：\r\n  python -m venv .venv\r\n  .\\.venv\\Scripts\\python.exe -m pip install -e \".[all]\"";
                return null;
            }
            string srcDir = Path.Combine(projectRoot, "src");
            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = string.Format("{0}-m web_crawler.app.ui --host 127.0.0.1 --port {1}", prefix, port),
                WorkingDirectory = projectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            string existingPyPath = Environment.GetEnvironmentVariable("PYTHONPATH") ?? "";
            psi.EnvironmentVariables["PYTHONPATH"] = string.IsNullOrEmpty(existingPyPath)
                ? srcDir
                : srcDir + ";" + existingPyPath;
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
            return psi;
        }
    }

    internal sealed class ChildJobObject : IDisposable
    {
        private IntPtr _handle;

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const int JobObjectExtendedLimitInformation = 9;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr hJob,
            int infoType,
            IntPtr lpJobObjectInfo,
            uint cbJobObjectInfoLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        public ChildJobObject()
        {
            _handle = CreateJobObject(IntPtr.Zero, null);
            if (_handle == IntPtr.Zero) return;

            var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr ptr = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(info, ptr, false);
                SetInformationJobObject(_handle, JobObjectExtendedLimitInformation, ptr, (uint)length);
            }
            finally
            {
                Marshal.FreeHGlobal(ptr);
            }
        }

        public void AddProcess(Process process)
        {
            if (_handle != IntPtr.Zero && process != null && !process.HasExited)
            {
                try { AssignProcessToJobObject(_handle, process.Handle); }
                catch { /* best effort; FormClosing also kills process tree */ }
            }
        }

        public void Dispose()
        {
            if (_handle != IntPtr.Zero)
            {
                CloseHandle(_handle);
                _handle = IntPtr.Zero;
            }
        }
    }

    internal sealed class ChildWebViewForm : Form
    {
        private readonly WebView2 _webView;
        private readonly CoreWebView2Environment _env;
        private readonly string _initialUrl;
        private readonly string _localOrigin;
        private readonly string _baseTitle;

        public ChildWebViewForm(
            string url,
            CoreWebView2Environment env,
            Icon icon,
            string baseTitle,
            string localOrigin)
        {
            _initialUrl = url;
            _env = env;
            _baseTitle = baseTitle;
            _localOrigin = localOrigin;

            Text = baseTitle;
            if (icon != null) Icon = icon;
            StartPosition = FormStartPosition.CenterScreen;
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            Width = Math.Min(1280, Math.Max(900, (int)(area.Width * 0.82)));
            Height = Math.Min(860, Math.Max(640, (int)(area.Height * 0.84)));
            BackColor = ColorTranslator.FromHtml(AppSpec.ThemeBgHex);

            _webView = new WebView2 { Dock = DockStyle.Fill };
            Controls.Add(_webView);
            Load += OnFormLoad;
        }

        private async void OnFormLoad(object sender, EventArgs e)
        {
            try
            {
                await _webView.EnsureCoreWebView2Async(_env);
                _webView.CoreWebView2.DocumentTitleChanged += (s, args) =>
                {
                    string t = _webView.CoreWebView2.DocumentTitle;
                    Text = string.IsNullOrWhiteSpace(t) ? _baseTitle : string.Format("{0} — {1}", t, _baseTitle);
                };
                _webView.CoreWebView2.NewWindowRequested += (s, args) =>
                {
                    if (!string.IsNullOrEmpty(args.Uri))
                    {
                        args.Handled = true;
                        if (args.Uri.StartsWith(_localOrigin, StringComparison.OrdinalIgnoreCase))
                        {
                            _webView.CoreWebView2.Navigate(args.Uri);
                        }
                        else if (args.Uri.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                                 args.Uri.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                        {
                            try { Process.Start(new ProcessStartInfo(args.Uri) { UseShellExecute = true }); }
                            catch { }
                        }
                    }
                };
                _webView.CoreWebView2.Navigate(_initialUrl);
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "无法打开页面：" + ex.Message, _baseTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        }
    }

    internal sealed class MainWindow : Form
    {
        private readonly string _projectRoot;
        private readonly bool _powerMode;
        private readonly bool _smokeTest;
        private readonly string _smokeOutputPath;

        private readonly Panel _splashPanel;
        private readonly Label _titleLabel;
        private readonly Label _statusLabel;
        private readonly ProgressBar _progressBar;
        private readonly TextBox _errorBox;
        private readonly FlowLayoutPanel _actionPanel;
        private readonly Button _retryButton;
        private readonly Button _copyLogButton;
        private readonly Button _openEnvButton;
        private readonly WebView2 _webView;

        private CoreWebView2Environment _webViewEnv;
        private ChildJobObject _job;
        private Process _serverProcess;
        private CancellationTokenSource _cts;
        private readonly object _logLock = new object();
        private readonly Queue<string> _logLines = new Queue<string>();
        private string _localOrigin = "";
        private bool _shuttingDown = false;
        private bool _webViewConfigured = false;
        public int ExitCode { get; private set; }

        public MainWindow(string projectRoot, bool powerMode, bool smokeTest, string smokeOutputPath)
        {
            _projectRoot = projectRoot;
            _powerMode = powerMode;
            _smokeTest = smokeTest;
            _smokeOutputPath = smokeOutputPath;
            ExitCode = 0;

            string title = AppSpec.WindowTitle;
            if (_powerMode && AppSpec.StripPowerModeByDefault)
            {
                title = title + " [POWER MODE]";
            }

            Text = title;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch { }

            StartPosition = FormStartPosition.CenterScreen;
            Rectangle area = Screen.PrimaryScreen.WorkingArea;
            Width = Math.Min(1440, Math.Max(1024, (int)(area.Width * 0.88)));
            Height = Math.Min(920, Math.Max(700, (int)(area.Height * 0.88)));
            MinimumSize = new Size(860, 580);
            BackColor = ColorTranslator.FromHtml(AppSpec.ThemeBgHex);

            _webView = new WebView2
            {
                Dock = DockStyle.Fill,
                Visible = false
            };

            _splashPanel = new Panel
            {
                Dock = DockStyle.Fill,
                BackColor = ColorTranslator.FromHtml(AppSpec.ThemeBgHex),
                Padding = new Padding(48)
            };

            var centerContainer = new TableLayoutPanel
            {
                Dock = DockStyle.Fill,
                ColumnCount = 1,
                RowCount = 6,
                BackColor = Color.Transparent
            };
            centerContainer.RowStyles.Add(new RowStyle(SizeType.Percent, 32f));
            centerContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            centerContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            centerContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            centerContainer.RowStyles.Add(new RowStyle(SizeType.Percent, 48f));
            centerContainer.RowStyles.Add(new RowStyle(SizeType.AutoSize));

            _titleLabel = new Label
            {
                Text = title,
                ForeColor = Color.White,
                Font = new Font("Microsoft YaHei UI", 18f, FontStyle.Bold),
                AutoSize = true,
                Anchor = AnchorStyles.None,
                Margin = new Padding(0, 0, 0, 12)
            };

            _statusLabel = new Label
            {
                Text = AppSpec.Subtitle,
                ForeColor = Color.FromArgb(203, 213, 225),
                Font = new Font("Microsoft YaHei UI", 10.5f, FontStyle.Regular),
                AutoSize = true,
                Anchor = AnchorStyles.None,
                Margin = new Padding(0, 0, 0, 18)
            };

            _progressBar = new ProgressBar
            {
                Style = ProgressBarStyle.Marquee,
                MarqueeAnimationSpeed = 28,
                Width = 360,
                Height = 8,
                Anchor = AnchorStyles.None,
                Margin = new Padding(0, 0, 0, 18)
            };

            _errorBox = new TextBox
            {
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Visible = false,
                Width = 760,
                Height = 240,
                Anchor = AnchorStyles.Top,
                BackColor = Color.FromArgb(30, 41, 59),
                ForeColor = Color.FromArgb(248, 250, 252),
                BorderStyle = BorderStyle.FixedSingle,
                Font = new Font("Consolas", 9.5f, FontStyle.Regular),
                Margin = new Padding(0, 8, 0, 16)
            };

            _actionPanel = new FlowLayoutPanel
            {
                AutoSize = true,
                FlowDirection = FlowDirection.LeftToRight,
                Anchor = AnchorStyles.None,
                Visible = false
            };

            _retryButton = CreateActionButton("重试启动", ColorTranslator.FromHtml(AppSpec.AccentHex));
            _retryButton.Click += (s, args) => { var _ = StartAppAsync(); };

            _copyLogButton = CreateActionButton("复制诊断日志", Color.FromArgb(51, 65, 85));
            _copyLogButton.Click += (s, args) =>
            {
                try { Clipboard.SetText(_errorBox.Text); }
                catch { }
            };

            _openEnvButton = CreateActionButton("打开 .env 配置", Color.FromArgb(15, 118, 110));
            _openEnvButton.Visible = false;
            _openEnvButton.Click += (s, args) =>
            {
                string envPath = Path.Combine(_projectRoot, ".env");
                try { Process.Start("notepad.exe", "\"" + envPath + "\""); }
                catch { }
            };

            _actionPanel.Controls.Add(_retryButton);
            _actionPanel.Controls.Add(_openEnvButton);
            _actionPanel.Controls.Add(_copyLogButton);

            centerContainer.Controls.Add(_titleLabel, 0, 1);
            centerContainer.Controls.Add(_statusLabel, 0, 2);
            centerContainer.Controls.Add(_progressBar, 0, 3);
            centerContainer.Controls.Add(_errorBox, 0, 4);
            centerContainer.Controls.Add(_actionPanel, 0, 5);

            _splashPanel.Controls.Add(centerContainer);
            Controls.Add(_webView);
            Controls.Add(_splashPanel);

            Load += (s, args) => { var _ = StartAppAsync(); };
            FormClosing += OnFormClosing;
        }

        private static Button CreateActionButton(string text, Color bg)
        {
            var btn = new Button
            {
                Text = text,
                AutoSize = true,
                FlatStyle = FlatStyle.Flat,
                BackColor = bg,
                ForeColor = Color.White,
                Font = new Font("Microsoft YaHei UI", 9.5f, FontStyle.Bold),
                Padding = new Padding(14, 7, 14, 7),
                Margin = new Padding(6, 0, 6, 0),
                Cursor = Cursors.Hand
            };
            btn.FlatAppearance.BorderSize = 0;
            return btn;
        }

        private void AppendLog(string prefix, string line)
        {
            if (string.IsNullOrEmpty(line)) return;
            lock (_logLock)
            {
                if (_logLines.Count >= 80) _logLines.Dequeue();
                _logLines.Enqueue(string.Format("[{0}] {1}", prefix, line));
            }
        }

        private string GetRecentLogs()
        {
            lock (_logLock)
            {
                return string.Join(Environment.NewLine, _logLines.ToArray());
            }
        }

        private static int FindFreeLoopbackPort(int[] preferredPorts)
        {
            if (preferredPorts != null)
            {
                foreach (int candidate in preferredPorts)
                {
                    if (candidate > 0 && IsPortBindable(candidate))
                    {
                        return candidate;
                    }
                }
            }
            var listener = new TcpListener(IPAddress.Loopback, 0);
            try
            {
                listener.Start();
                return ((IPEndPoint)listener.LocalEndpoint).Port;
            }
            finally
            {
                listener.Stop();
            }
        }

        private static bool IsPortBindable(int port)
        {
            TcpListener listener = null;
            try
            {
                listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();
                return true;
            }
            catch
            {
                return false;
            }
            finally
            {
                if (listener != null)
                {
                    try { listener.Stop(); } catch { }
                }
            }
        }

        private static void ApplyDotEnv(ProcessStartInfo psi, string dotEnvPath)
        {
            if (!File.Exists(dotEnvPath)) return;
            string[] lines = File.ReadAllLines(dotEnvPath, Encoding.UTF8);
            foreach (string rawLine in lines)
            {
                string line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string key = line.Substring(0, eq).Trim();
                string val = line.Substring(eq + 1).Trim();
                if (val.Length >= 2 &&
                    ((val.StartsWith("\"") && val.EndsWith("\"")) ||
                     (val.StartsWith("'") && val.EndsWith("'"))))
                {
                    val = val.Substring(1, val.Length - 2);
                }
                if (!string.IsNullOrEmpty(key))
                {
                    psi.EnvironmentVariables[key] = val;
                }
            }
        }

        private void ShowError(string headline, string detail, bool showOpenEnv)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action(() => ShowError(headline, detail, showOpenEnv)));
                return;
            }
            _progressBar.Visible = false;
            _statusLabel.ForeColor = Color.FromArgb(252, 165, 165);
            _statusLabel.Text = headline;
            _errorBox.Text = detail;
            _errorBox.Visible = true;
            _openEnvButton.Visible = showOpenEnv;
            _actionPanel.Visible = true;
            _splashPanel.Visible = true;
            _webView.Visible = false;

            if (_smokeTest)
            {
                WriteSmokeResult(false, headline + " | " + detail);
                ExitCode = 1;
                Close();
            }
        }

        private void UpdateStatus(string text)
        {
            if (InvokeRequired)
            {
                BeginInvoke(new Action(() => UpdateStatus(text)));
                return;
            }
            _statusLabel.Text = text;
        }

        private void WriteSmokeResult(bool ok, string message)
        {
            if (string.IsNullOrEmpty(_smokeOutputPath)) return;
            try
            {
                string payload = string.Format(
                    "{{\"ok\":{0},\"app\":\"{1}\",\"origin\":\"{2}\",\"message\":\"{3}\"}}",
                    ok ? "true" : "false",
                    EscapeJson(AppSpec.AppSlug),
                    EscapeJson(_localOrigin),
                    EscapeJson(message));
                File.WriteAllText(_smokeOutputPath, payload, Encoding.UTF8);
            }
            catch { }
        }

        private static string EscapeJson(string s)
        {
            if (s == null) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n");
        }

        private async Task StartAppAsync()
        {
            StopServer();
            lock (_logLock) { _logLines.Clear(); }
            _cts = new CancellationTokenSource();

            _progressBar.Visible = true;
            _errorBox.Visible = false;
            _actionPanel.Visible = false;
            _statusLabel.ForeColor = Color.FromArgb(203, 213, 225);
            _statusLabel.Text = AppSpec.Subtitle;

            if (AppSpec.RequireDotEnv)
            {
                string envFile = Path.Combine(_projectRoot, ".env");
                string envExample = Path.Combine(_projectRoot, ".env.example");
                if (!File.Exists(envFile))
                {
                    if (File.Exists(envExample))
                    {
                        try { File.Copy(envExample, envFile, false); }
                        catch { }
                        ShowError(
                            "首次运行：已自动从 .env.example 创建 .env",
                            "请点击下方「打开 .env 配置」，填入 OPENAI_API_KEY 后保存，再点击「重试启动」。\r\n文件位置：" + envFile,
                            true);
                        return;
                    }
                    ShowError("缺少 .env 配置文件", "项目根目录缺少 .env 与 .env.example：" + _projectRoot, false);
                    return;
                }
            }

            int port = FindFreeLoopbackPort(AppSpec.PreferredPorts);
            _localOrigin = string.Format("http://127.0.0.1:{0}", port);

            string setupError;
            string setupHint;
            ProcessStartInfo psi = AppSpec.BuildServerStartInfo(_projectRoot, port, _powerMode, out setupError, out setupHint);
            if (psi == null)
            {
                ShowError(setupError ?? "环境检查未通过", setupHint ?? "", false);
                return;
            }

            if (AppSpec.LoadDotEnv)
            {
                ApplyDotEnv(psi, Path.Combine(_projectRoot, ".env"));
            }
            if (AppSpec.StripPowerModeByDefault)
            {
                if (_powerMode)
                {
                    psi.EnvironmentVariables["WEB_CRAWLER_POWER_MODE"] = "1";
                }
                else
                {
                    psi.EnvironmentVariables.Remove("WEB_CRAWLER_POWER_MODE");
                    psi.EnvironmentVariables.Remove("WEB_CRAWLER_ALLOW_PRIVATE_HOSTS");
                }
            }

            try
            {
                _job = new ChildJobObject();
                _serverProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
                _serverProcess.OutputDataReceived += (s, args) => AppendLog("stdout", args.Data);
                _serverProcess.ErrorDataReceived += (s, args) => AppendLog("stderr", args.Data);
                _serverProcess.Start();
                _job.AddProcess(_serverProcess);
                _serverProcess.BeginOutputReadLine();
                _serverProcess.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                ShowError("无法启动后台服务进程", ex.ToString(), false);
                return;
            }

            UpdateStatus(string.Format("正在初始化本地界面（127.0.0.1:{0}）…", port));

            try
            {
                if (_webViewEnv == null)
                {
                    string userData = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                        AppSpec.AppSlug,
                        "WebView2");
                    Directory.CreateDirectory(userData);
                    _webViewEnv = await CoreWebView2Environment.CreateAsync(null, userData);
                }
                await _webView.EnsureCoreWebView2Async(_webViewEnv);
                ConfigureWebView();
            }
            catch (WebView2RuntimeNotFoundException)
            {
                StopServer();
                ShowError(
                    "未检测到 Microsoft Edge WebView2 Runtime",
                    "请先安装微软官方 WebView2 Runtime（Windows 10/11 通常已内置）：\r\nhttps://developer.microsoft.com/microsoft-edge/webview2/",
                    false);
                return;
            }
            catch (Exception ex)
            {
                StopServer();
                ShowError("初始化桌面渲染引擎失败", ex.ToString(), false);
                return;
            }

            UpdateStatus(string.Format("正在等待本地服务就绪（{0}）…", _localOrigin));
            bool ready = await WaitForHealthyAsync(_localOrigin + AppSpec.HealthPath, _cts.Token);
            if (!ready)
            {
                if (_shuttingDown) return;
                string logs = GetRecentLogs();
                string reason = (_serverProcess != null && _serverProcess.HasExited)
                    ? string.Format("后台服务已提前退出（退出码 {0}）", _serverProcess.ExitCode)
                    : string.Format("等待服务就绪超时（{0} 秒）", AppSpec.StartupTimeoutSeconds);
                StopServer();
                ShowError(
                    reason,
                    string.IsNullOrWhiteSpace(logs) ? "无控制台输出，请检查本地依赖与 .env 配置。" : logs,
                    File.Exists(Path.Combine(_projectRoot, ".env")));
                return;
            }

            if (_shuttingDown) return;
            _webView.Visible = true;
            _splashPanel.Visible = false;
            _webView.CoreWebView2.Navigate(_localOrigin + "/");
        }

        private void ConfigureWebView()
        {
            if (_webViewConfigured) return;
            _webViewConfigured = true;

            _webView.DefaultBackgroundColor = ColorTranslator.FromHtml(AppSpec.ThemeBgHex);
            _webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            _webView.CoreWebView2.Settings.AreDevToolsEnabled = true;
            _webView.CoreWebView2.Settings.IsStatusBarEnabled = false;

            _webView.CoreWebView2.NewWindowRequested += (s, args) =>
            {
                if (string.IsNullOrEmpty(args.Uri)) return;
                args.Handled = true;
                if (args.Uri.StartsWith(_localOrigin, StringComparison.OrdinalIgnoreCase))
                {
                    var child = new ChildWebViewForm(args.Uri, _webViewEnv, this.Icon, AppSpec.WindowTitle, _localOrigin);
                    child.Show(this);
                }
                else if (args.Uri.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                         args.Uri.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                {
                    try { Process.Start(new ProcessStartInfo(args.Uri) { UseShellExecute = true }); }
                    catch { }
                }
            };

            _webView.CoreWebView2.NavigationCompleted += (s, args) =>
            {
                if (!_smokeTest) return;
                if (args.IsSuccess)
                {
                    string docTitle = _webView.CoreWebView2.DocumentTitle ?? "";
                    WriteSmokeResult(true, "Loaded: " + docTitle);
                    ExitCode = 0;
                }
                else
                {
                    WriteSmokeResult(false, "Navigation failed: " + args.WebErrorStatus.ToString());
                    ExitCode = 1;
                }
                BeginInvoke(new Action(Close));
            };
        }

        private async Task<bool> WaitForHealthyAsync(string healthUrl, CancellationToken token)
        {
            using (var handler = new HttpClientHandler { UseProxy = false })
            using (var client = new HttpClient(handler))
            {
                client.Timeout = TimeSpan.FromSeconds(2);
                DateTime deadline = DateTime.UtcNow.AddSeconds(AppSpec.StartupTimeoutSeconds);
                while (DateTime.UtcNow < deadline && !token.IsCancellationRequested)
                {
                    if (_serverProcess != null && _serverProcess.HasExited)
                    {
                        return false;
                    }
                    try
                    {
                        using (HttpResponseMessage resp = await client.GetAsync(healthUrl, token))
                        {
                            int code = (int)resp.StatusCode;
                            if (code >= 200 && code < 300) return true;
                            if (AppSpec.AcceptStatus503 && code == 503) return true;
                        }
                    }
                    catch
                    {
                        // Server still starting
                    }
                    try { await Task.Delay(350, token); }
                    catch (TaskCanceledException) { return false; }
                }
            }
            return false;
        }

        private void StopServer()
        {
            if (_cts != null)
            {
                try { _cts.Cancel(); } catch { }
                _cts.Dispose();
                _cts = null;
            }

            if (_serverProcess != null)
            {
                try
                {
                    if (!_serverProcess.HasExited)
                    {
                        int pid = _serverProcess.Id;
                        try
                        {
                            var kill = new ProcessStartInfo
                            {
                                FileName = "taskkill.exe",
                                Arguments = string.Format("/PID {0} /T /F", pid),
                                UseShellExecute = false,
                                CreateNoWindow = true
                            };
                            using (Process kp = Process.Start(kill))
                            {
                                if (kp != null) kp.WaitForExit(3000);
                            }
                        }
                        catch
                        {
                            try { _serverProcess.Kill(); } catch { }
                        }
                    }
                }
                catch { }
                finally
                {
                    _serverProcess.Dispose();
                    _serverProcess = null;
                }
            }

            if (_job != null)
            {
                _job.Dispose();
                _job = null;
            }
        }

        private void OnFormClosing(object sender, FormClosingEventArgs e)
        {
            _shuttingDown = true;
            StopServer();
        }
    }

    internal static class Program
    {
        [DllImport("user32.dll")]
        private static extern bool SetProcessDPIAware();

        [DllImport("user32.dll")]
        private static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);

        private static string ResolveProjectRoot()
        {
            string exeDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\', '/');
            string current = exeDir;
            for (int i = 0; i < 4 && !string.IsNullOrEmpty(current); i++)
            {
                if (File.Exists(Path.Combine(current, AppSpec.MarkerFile)))
                {
                    return current;
                }
                DirectoryInfo parent = Directory.GetParent(current);
                current = parent != null ? parent.FullName : null;
            }
            return exeDir;
        }

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                if (!SetProcessDpiAwarenessContext((IntPtr)(-4)))
                {
                    SetProcessDPIAware();
                }
            }
            catch
            {
                try { SetProcessDPIAware(); } catch { }
            }

            bool powerMode = false;
            bool smokeTest = false;
            string smokeOutput = null;

            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i] ?? "";
                if (string.Equals(a, "--power", StringComparison.OrdinalIgnoreCase))
                {
                    powerMode = true;
                }
                else if (string.Equals(a, "--smoke-test", StringComparison.OrdinalIgnoreCase))
                {
                    smokeTest = true;
                }
                else if (a.StartsWith("--smoke-output=", StringComparison.OrdinalIgnoreCase))
                {
                    smokeOutput = a.Substring("--smoke-output=".Length);
                }
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string root = ResolveProjectRoot();
            using (var win = new MainWindow(root, powerMode, smokeTest, smokeOutput))
            {
                Application.Run(win);
                return win.ExitCode;
            }
        }
    }
}
