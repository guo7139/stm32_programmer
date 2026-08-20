using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace Stm32Prog
{
    sealed class AppConfig
    {
        readonly string path;
        readonly Dictionary<string, JsonElement> data = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        static readonly HashSet<string> Allowed = new HashSet<string>(new[] { "username", "model_code", "part_no", "purpose", "program", "status", "aircraft_no", "eo_no" }, StringComparer.Ordinal);
        public AppConfig(string customPath=null){path=customPath??Path.Combine(AppContext.BaseDirectory,"stm32_programmer_config.json");try{if(!File.Exists(path))return;using var doc=JsonDocument.Parse(File.ReadAllText(path,Encoding.UTF8));if(doc.RootElement.ValueKind!=JsonValueKind.Object)return;foreach(var x in doc.RootElement.EnumerateObject())if(Allowed.Contains(x.Name))data[x.Name]=x.Value.Clone();}catch{data.Clear();}}
        public string Get(string key,string fallback=""){if(!data.TryGetValue(key,out var v)||v.ValueKind==JsonValueKind.Null)return fallback;return v.ValueKind==JsonValueKind.String?(v.GetString()??fallback):v.ToString();}
        public int GetInt(string key,int fallback){return int.TryParse(Get(key),out var n)?n:fallback;}
        public void Update(params (string Key,object Value)[] values){foreach(var x in values)if(Allowed.Contains(x.Key)){using var d=JsonDocument.Parse(JsonSerializer.Serialize(x.Value));data[x.Key]=d.RootElement.Clone();}Save();}
        void Save(){string tmp=path+"."+Guid.NewGuid().ToString("N")+".tmp";try{var ordered=new Dictionary<string,object>();foreach(var k in Allowed.OrderBy(x=>x,StringComparer.Ordinal))if(data.TryGetValue(k,out var v))ordered[k]=v;File.WriteAllText(tmp,JsonSerializer.Serialize(ordered,new JsonSerializerOptions{WriteIndented=true}),new UTF8Encoding(false));File.Move(tmp,path,true);}finally{try{if(File.Exists(tmp))File.Delete(tmp);}catch{}}}
    }

    sealed class AuthClient : IDisposable
    {
        const string BaseUrl = "http://192.168.60.241:9100";
        readonly HttpClient http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
        public string Token { get; private set; }
        public int UserId { get; private set; }
        public string Username { get; private set; }
        public JsonElement ApiLimit { get; private set; }
        public bool LoggedIn => !string.IsNullOrWhiteSpace(Token);
        public bool AfterSales => ApiLimit.ValueKind == JsonValueKind.Object &&
            ApiLimit.TryGetProperty("after_sales", out var v) && v.ValueKind == JsonValueKind.True;

        public async Task LoginAsync(string username, string password)
        {
            var result = await SendJsonAsync(HttpMethod.Post, "/api/login", new { username, password }, false);
            if (!result.TryGetProperty("ok", out var ok) || ok.ValueKind != JsonValueKind.True)
                throw new Exception(MessageOf(result, "用户名或密码错误"));
            if (!result.TryGetProperty("api_token", out var token) || string.IsNullOrWhiteSpace(token.GetString()))
                throw new Exception("登录结果缺少api_token");
            Token = token.GetString(); Username = username;
            UserId = result.TryGetProperty("id", out var id) && id.TryGetInt32(out var uid) ? uid : 0;
            ApiLimit = result.TryGetProperty("api_limit", out var lim) && lim.ValueKind == JsonValueKind.Object
                ? lim.Clone() : default;
        }

        public List<string> AllowedPrograms()
        {
            var all = new[] { "BootLoader", "App", "Parameter" };
            var set = StringArrayLimit("program");
            return set.Count == 0 ? all.ToList() : all.Where(set.Contains).ToList();
        }
        public List<(int Value, string Label)> AllowedStatuses()
        {
            var all = new List<(int,string)> { (0,"研发验证"), (1,"局方批准"), (2,"生产测试") };
            var set = IntArrayLimit("status");
            return set.Count == 0 ? all : all.Where(x => set.Contains(x.Item1)).ToList();
        }
        HashSet<string> StringArrayLimit(string name)
        {
            var r = new HashSet<string>(StringComparer.Ordinal);
            if (ApiLimit.ValueKind == JsonValueKind.Object && ApiLimit.TryGetProperty(name, out var a) && a.ValueKind == JsonValueKind.Array)
                foreach (var x in a.EnumerateArray()) if (x.ValueKind == JsonValueKind.String) r.Add(x.GetString());
            return r;
        }
        HashSet<int> IntArrayLimit(string name)
        {
            var r = new HashSet<int>();
            if (ApiLimit.ValueKind == JsonValueKind.Object && ApiLimit.TryGetProperty(name, out var a) && a.ValueKind == JsonValueKind.Array)
                foreach (var x in a.EnumerateArray()) { if (x.TryGetInt32(out int n)) r.Add(n); else if (x.ValueKind == JsonValueKind.String && int.TryParse(x.GetString(), out n)) r.Add(n); }
            return r;
        }

        HttpRequestMessage Request(HttpMethod method, string path, bool tokenQuery = false)
        {
            if (tokenQuery && LoggedIn) path += (path.Contains("?") ? "&" : "?") + "token=" + Uri.EscapeDataString(Token);
            var req = new HttpRequestMessage(method, BaseUrl + path);
            req.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            if (LoggedIn) { req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", Token); req.Headers.TryAddWithoutValidation("api_token", Token); }
            return req;
        }
        public async Task<JsonElement> SendJsonAsync(HttpMethod method, string path, object body = null, bool tokenQuery = true)
        {
            using var req = Request(method, path, tokenQuery);
            if (body != null) req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
            using var resp = await http.SendAsync(req);
            string text = await resp.Content.ReadAsStringAsync();
            JsonElement root = default;
            try { root = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text).RootElement.Clone(); } catch { }
            if (!resp.IsSuccessStatusCode) throw new Exception(root.ValueKind == JsonValueKind.Object ? MessageOf(root, $"HTTP {(int)resp.StatusCode}") : $"HTTP {(int)resp.StatusCode}: {text}");
            return root;
        }
        public async Task<List<JsonElement>> GetModelsAsync()
        {
            var root = await SendJsonAsync(HttpMethod.Get, "/api/parts", null, true);
            var rows = AsList(root); var map = new Dictionary<string,JsonElement>();
            foreach (var x in rows) if (x.ValueKind == JsonValueKind.Object && x.TryGetProperty("model_code", out var c)) { var k=c.GetString()??""; if(k.Length>0&&!map.ContainsKey(k)) map[k]=x.Clone(); }
            return map.Values.ToList();
        }
        public async Task<List<JsonElement>> GetPartsAsync(string model)
        {
            var root = await SendJsonAsync(HttpMethod.Get, "/api/parts?model_code=" + Uri.EscapeDataString(model), null, true);
            return AsList(root).Where(x => Text(x,"model_code")==model && Text(x,"part_no").Length>0).Select(x=>x.Clone()).ToList();
        }
        public async Task<JsonElement?> LatestAsync(string model,string part,string purpose,string program,int status,string aircraft,string eo)
        {
            var q = new Dictionary<string,string>{{"model_code",model},{"part_no",part},{"purpose",purpose},{"program",program},{"status",status.ToString()}};
            if(!string.IsNullOrWhiteSpace(aircraft))q["aircraft_no"]=aircraft.Trim(); if(!string.IsNullOrWhiteSpace(eo))q["eo_no"]=eo.Trim();
            string path="/openapi/versions/latest?"+string.Join("&",q.Select(x=>Uri.EscapeDataString(x.Key)+"="+Uri.EscapeDataString(x.Value)));
            var r=await SendJsonAsync(HttpMethod.Get,path,null,true);
            if(r.ValueKind==JsonValueKind.Null || (r.ValueKind==JsonValueKind.Object&&!r.EnumerateObject().Any())) return null;
            if(r.ValueKind==JsonValueKind.Object&&r.TryGetProperty("data",out var d)){ if(d.ValueKind==JsonValueKind.Null)return null; return d.Clone(); }
            return r.Clone();
        }
        public async Task<string> DownloadAsync(JsonElement version, Action<string> log)
        {
            string id = Text(version, "id");
            string name = Path.GetFileName(Text(version, "file_name"));
            if (id.Length == 0) throw new Exception("固件版本缺少id");
            if (name.Length == 0) name = "firmware-" + id + ".bin";
            name = string.Concat(name.Select(c => char.IsLetterOrDigit(c) || "._-".Contains(c) ? c : '_'));
            string expectedMd5 = Text(version, "file_md5").ToLowerInvariant();
            if (expectedMd5.Length == 0) throw new Exception("固件MD5为空");
            if (!long.TryParse(Text(version, "file_size"), out long expectedSize) || expectedSize < 0)
                throw new Exception("固件file_size无效");

            string directory = Path.Combine(Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData), "stm32_programmer", "firmware");
            Directory.CreateDirectory(directory);
            string temp = Path.Combine(directory, "download-" + Guid.NewGuid().ToString("N") + ".tmp");
            string destination = Path.Combine(directory, name);
            try
            {
                long total = 0;
                string actualMd5;
                // 所有持有临时文件的资源必须在移动文件、启动烧录之前关闭。
                using (var request = Request(HttpMethod.Get,
                    "/openapi/download/" + Uri.EscapeDataString(id), true))
                {
                    request.Headers.Accept.Clear();
                    request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/octet-stream"));
                    using (var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead))
                    {
                        if (!response.IsSuccessStatusCode)
                            throw new Exception("下载服务器返回 HTTP " + (int)response.StatusCode);
                        using (var input = await response.Content.ReadAsStreamAsync())
                        using (var output = new FileStream(temp, FileMode.Create, FileAccess.Write,
                            FileShare.None, 262144, true))
                        using (var hash = MD5.Create())
                        {
                            byte[] buffer = new byte[262144];
                            int count;
                            while ((count = await input.ReadAsync(buffer, 0, buffer.Length)) > 0)
                            {
                                await output.WriteAsync(buffer, 0, count);
                                hash.TransformBlock(buffer, 0, count, null, 0);
                                total += count;
                                log($"下载: {total}/{expectedSize} 字节\r");
                            }
                            hash.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                            await output.FlushAsync();
                            actualMd5 = BitConverter.ToString(hash.Hash).Replace("-", "").ToLowerInvariant();
                        }
                    }
                }

                var errors = new List<string>();
                if (total != expectedSize) errors.Add($"大小不符，期望{expectedSize}，实际{total}");
                if (actualMd5 != expectedMd5) errors.Add($"MD5不符，期望{expectedMd5}，实际{actualMd5}");
                if (errors.Count > 0) throw new Exception(string.Join("；", errors));

                File.Move(temp, destination, true);
                // 在交给烧录器之前验证最终文件已解除独占锁。
                using (var check = new FileStream(destination, FileMode.Open, FileAccess.Read,
                    FileShare.Read, 1, FileOptions.SequentialScan)) { }
                log($"[✓] 固件下载校验通过: {destination}\n");
                return destination;
            }
            catch
            {
                try { if (File.Exists(temp)) File.Delete(temp); } catch { }
                throw;
            }
        }
        public async Task SubmitBurnAsync(JsonElement version,bool success)
        {
            string id=Text(version,"id"); if(UserId<=0||id.Length==0)throw new Exception("缺少用户id或版本id");
            await SendJsonAsync(HttpMethod.Post,"/openapi/burn-records",new {user_id=UserId,version_id=int.TryParse(id,out int n)?n:(object)id,success},true);
        }
        public static string Text(JsonElement x,string name){ if(x.ValueKind!=JsonValueKind.Object||!x.TryGetProperty(name,out var v)||v.ValueKind==JsonValueKind.Null)return ""; return v.ValueKind==JsonValueKind.String?v.GetString():v.ToString(); }
        static List<JsonElement> AsList(JsonElement r){ if(r.ValueKind==JsonValueKind.Array)return r.EnumerateArray().Select(x=>x.Clone()).ToList(); if(r.ValueKind==JsonValueKind.Object)foreach(var k in new[]{"data","list","items","rows"})if(r.TryGetProperty(k,out var a)&&a.ValueKind==JsonValueKind.Array)return a.EnumerateArray().Select(x=>x.Clone()).ToList(); return new List<JsonElement>(); }
        static string MessageOf(JsonElement r,string fallback){ foreach(var k in new[]{"message","error","detail"})if(r.TryGetProperty(k,out var v)&&v.ValueKind==JsonValueKind.String&&!string.IsNullOrWhiteSpace(v.GetString()))return v.GetString(); return fallback; }
        public void Dispose()=>http.Dispose();
    }

    sealed class LoginForm : Form
    {
        readonly TextBox user=new TextBox(),pass=new TextBox();readonly Label state=new Label();readonly Button login=new Button();readonly AuthClient auth;readonly AppConfig config;
        public LoginForm(AuthClient a,AppConfig c){auth=a;config=c;Text="STM32 ST-Link 烧录工具 - 用户登录";ClientSize=new Size(460,245);FormBorderStyle=FormBorderStyle.FixedDialog;MaximizeBox=false;StartPosition=FormStartPosition.CenterScreen;Controls.Add(new Label{Text="用户登录",Font=new Font(Font,FontStyle.Bold),Location=new Point(28,22),AutoSize=true});Controls.Add(new Label{Text="服务器：",Location=new Point(35,62),AutoSize=true});Controls.Add(new Label{Text="http://192.168.60.241:9100",Location=new Point(115,62),AutoSize=true});Controls.Add(new Label{Text="用户名：",Location=new Point(35,97),AutoSize=true});user.SetBounds(115,92,295,27);user.Text=config.Get("username");Controls.Add(user);Controls.Add(new Label{Text="密码：",Location=new Point(35,132),AutoSize=true});pass.SetBounds(115,127,295,27);pass.UseSystemPasswordChar=true;Controls.Add(pass);state.SetBounds(35,165,375,24);state.Text="请输入用户名和密码";Controls.Add(state);login.Text="登录";login.SetBounds(220,197,90,32);login.Click+=async(_,__)=>await DoLogin();Controls.Add(login);var cancel=new Button{Text="取消"};cancel.SetBounds(320,197,90,32);cancel.Click+=(_,__)=>Close();Controls.Add(cancel);AcceptButton=login;CancelButton=cancel;Shown+=(_,__)=>(user.TextLength>0?pass:user).Focus();}
        async Task DoLogin(){if(string.IsNullOrWhiteSpace(user.Text)||pass.Text.Length==0){state.Text="请输入用户名和密码";return;}Toggle(false);state.Text="正在连接登录服务器...";try{await auth.LoginAsync(user.Text.Trim(),pass.Text);config.Update(("username",auth.Username));DialogResult=DialogResult.OK;Close();}catch(Exception e){state.Text="登录失败："+e.Message;pass.SelectAll();pass.Focus();}finally{Toggle(true);}}
        void Toggle(bool v){user.Enabled=pass.Enabled=login.Enabled=v;}
    }

    sealed class LogWriter : TextWriter
    { readonly Action<string> write; public LogWriter(Action<string> w){write=w;} public override Encoding Encoding=>Encoding.UTF8; public override void Write(char c)=>write(c.ToString()); public override void Write(string s)=>write(s); public override void WriteLine(string s)=>write((s??"")+Environment.NewLine); }

    sealed class ProgressForm : Form
    {
        readonly TextBox log=new TextBox(); readonly Button retry=new Button(), close=new Button(); readonly Func<Action<string>,Task<bool>> operation; bool running,success;
        public ProgressForm(string title,string action,Func<Action<string>,Task<bool>> op){operation=op;Text=title;ClientSize=new Size(760,520);MinimumSize=new Size(620,400);StartPosition=FormStartPosition.CenterParent;
            log.Multiline=true;log.ReadOnly=true;log.ScrollBars=ScrollBars.Both;log.WordWrap=false;log.Font=new Font("Consolas",10);log.BackColor=Color.FromArgb(16,24,32);log.ForeColor=Color.FromArgb(232,241,242);log.SetBounds(12,12,720,420);log.Anchor=AnchorStyles.Top|AnchorStyles.Bottom|AnchorStyles.Left|AnchorStyles.Right;Controls.Add(log);
            retry.Text=action;retry.SetBounds(540,450,90,32);retry.Anchor=AnchorStyles.Bottom|AnchorStyles.Right;retry.Click+=async(_,__)=>await Run();Controls.Add(retry);close.Text="关闭";close.SetBounds(640,450,90,32);close.Anchor=AnchorStyles.Bottom|AnchorStyles.Right;close.Enabled=false;close.Click+=(_,__)=>Close();Controls.Add(close);Shown+=async(_,__)=>await Run();FormClosing+=(_,e)=>{if(running)e.Cancel=true;};}
        void Append(string s){if(InvokeRequired){BeginInvoke(new Action<string>(Append),s);return;} if(s.Contains('\r')&&!s.Contains('\n')){int i=log.Text.LastIndexOf('\n');log.Text=(i>=0?log.Text.Substring(0,i+1):"")+s.Replace("\r","");}else log.AppendText(s);log.SelectionStart=log.TextLength;log.ScrollToCaret();}
        async Task Run(){if(running||success)return;running=true;retry.Enabled=close.Enabled=false;try{success=await operation(Append);}finally{running=false;retry.Enabled=!success;close.Enabled=true;}}
    }

    sealed class FirmwareForm : Form
    {
        readonly AuthClient auth;readonly AppConfig config;readonly ComboBox model=new ComboBox(),part=new ComboBox(),chip=new ComboBox(),program=new ComboBox(),status=new ComboBox();readonly TextBox aircraft=new TextBox(),eo=new TextBox();readonly Label modelLabel=new Label(),partLabel=new Label(),chipLabel=new Label(),programLabel=new Label(),statusLabel=new Label(),aircraftLabel=new Label(),eoLabel=new Label(),message=new Label();readonly Button query=new Button(),erase=new Button(),burn=new Button(),cancel=new Button();readonly GroupBox box=new GroupBox();readonly Dictionary<string,Label> info=new Dictionary<string,Label>();List<JsonElement> models=new List<JsonElement>(),parts=new List<JsonElement>();JsonElement? version;string burnAddress;bool restoring=true;
        public FirmwareForm(AuthClient a,AppConfig c){auth=a;config=c;Text="STM32 ST-Link 烧录工具 - 固件选择";ClientSize=new Size(720,690);MinimumSize=new Size(700,650);StartPosition=FormStartPosition.CenterScreen;AutoScaleMode=AutoScaleMode.Dpi;Build();Shown+=async(_,__)=>await LoadModels();}
        void Build(){modelLabel.Text="机型：";partLabel.Text="零部件：";chipLabel.Text="芯片：";programLabel.Text="类型：";statusLabel.Text="状态：";aircraftLabel.Text="航空器编号：";eoLabel.Text="EO单号：";foreach(var l in new[]{modelLabel,partLabel,chipLabel,programLabel,statusLabel,aircraftLabel,eoLabel}){l.TextAlign=ContentAlignment.MiddleRight;Controls.Add(l);}foreach(var c in new Control[]{model,part,chip,program,status,aircraft,eo})Controls.Add(c);foreach(var x in auth.AllowedPrograms())program.Items.Add(x);foreach(var x in auth.AllowedStatuses())status.Items.Add(new StatusItem(x.Value,x.Label));SelectText(program,config.Get("program","BootLoader"));SelectStatus(config.GetInt("status",1));if(auth.AfterSales){aircraft.Text=config.Get("aircraft_no");eo.Text=config.Get("eo_no");}query.Text="查询固件";query.Click+=async(_,__)=>await Query();Controls.Add(query);erase.Text="全片擦除";erase.BackColor=Color.Firebrick;erase.ForeColor=Color.White;erase.Click+=(_,__)=>Erase();Controls.Add(erase);message.Text="正在加载机型...";Controls.Add(message);box.Text="固件版本信息";Controls.Add(box);int iy=28;foreach(var x in new[]{("文件名","file_name"),("MD5","file_md5"),("文件大小","file_size"),("版本","version"),("烧录地址","burn_addr")}){box.Controls.Add(new Label{Text=x.Item1+"：",Location=new Point(18,iy),AutoSize=true});var l=new Label{Text="-",Location=new Point(110,iy),AutoSize=true,MaximumSize=new Size(500,0)};box.Controls.Add(l);info[x.Item2]=l;iy+=30;}burn.Text="烧录";burn.Enabled=false;burn.Click+=(_,__)=>Burn();Controls.Add(burn);cancel.Text="取消";cancel.Click+=(_,__)=>Close();Controls.Add(cancel);model.DropDownStyle=part.DropDownStyle=chip.DropDownStyle=program.DropDownStyle=status.DropDownStyle=ComboBoxStyle.DropDownList;model.SelectedIndexChanged+=async(_,__)=>{if(!restoring)await LoadParts();};part.SelectedIndexChanged+=(_,__)=>{if(!restoring)LoadChips();};chip.SelectedIndexChanged+=(_,__)=>{if(!restoring)ClearVersion();};program.SelectedIndexChanged+=(_,__)=>{if(!restoring)ClearVersion();};status.SelectedIndexChanged+=(_,__)=>{if(!restoring)ClearVersion();};aircraft.TextChanged+=(_,__)=>{if(!restoring)ClearVersion();};eo.TextChanged+=(_,__)=>{if(!restoring)ClearVersion();};Relayout();}
        void Place(Label l,Control c,int y){l.SetBounds(35,y+2,120,28);c.SetBounds(165,y,475,28);c.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;}
        void Relayout(){int y=22;Place(modelLabel,model,y);y+=38;Place(partLabel,part,y);y+=38;bool hc=chip.Items.Count>0;chipLabel.Visible=chip.Visible=hc;if(hc){Place(chipLabel,chip,y);y+=38;}Place(programLabel,program,y);y+=38;Place(statusLabel,status,y);y+=38;aircraftLabel.Visible=aircraft.Visible=eoLabel.Visible=eo.Visible=auth.AfterSales;if(auth.AfterSales){Place(aircraftLabel,aircraft,y);y+=38;Place(eoLabel,eo,y);y+=38;}query.SetBounds(165,y+8,105,32);erase.SetBounds(530,y+8,110,32);y+=52;message.SetBounds(30,y,650,26);y+=32;box.SetBounds(28,y,645,190);box.Anchor=AnchorStyles.Top|AnchorStyles.Left|AnchorStyles.Right;y+=208;burn.SetBounds(470,y,90,32);cancel.SetBounds(570,y,90,32);int need=y+55;if(ClientSize.Height<need)ClientSize=new Size(ClientSize.Width,need);}
        static string DisplayName(JsonElement x,string codeKey,string nameKey){string code=AuthClient.Text(x,codeKey).Trim();string name=AuthClient.Text(x,nameKey).Trim();if(name.Length==0){var keys=codeKey=="model_code"?new[]{"modelName","name"}:new[]{"partName","name"};foreach(var k in keys){name=AuthClient.Text(x,k).Trim();if(name.Length>0)break;}}return name.Length>0&&name!=code?$"{code} - {name}":code;}
        async Task LoadModels(){try{models=await auth.GetModelsAsync();model.Items.Clear();foreach(var x in models)model.Items.Add(new JsonItem(x,DisplayName(x,"model_code","model_name")));SelectJson(model,"model_code",config.Get("model_code"));if(model.SelectedIndex<0&&model.Items.Count>0)model.SelectedIndex=0;await LoadParts();message.Text=$"已加载 {model.Items.Count} 个机型";}catch(Exception e){message.Text="加载机型失败："+e.Message;}}
        async Task LoadParts(){ClearVersion();if(!(model.SelectedItem is JsonItem m))return;try{parts=await auth.GetPartsAsync(AuthClient.Text(m.Value,"model_code"));part.Items.Clear();foreach(var x in parts)part.Items.Add(new JsonItem(x,DisplayName(x,"part_no","part_name")));SelectJson(part,"part_no",config.Get("part_no"));if(part.SelectedIndex<0&&part.Items.Count>0)part.SelectedIndex=0;LoadChips();message.Text=$"已加载 {part.Items.Count} 个零部件";}catch(Exception e){message.Text="加载零部件失败："+e.Message;}}
        void LoadChips(){ClearVersion();chip.Items.Clear();if(part.SelectedItem is JsonItem p)foreach(var x in AuthClient.Text(p.Value,"purpose").Split('|').Select(x=>x.Trim()).Where(x=>x.Length>0))chip.Items.Add(x);SelectText(chip,config.Get("purpose"));if(chip.SelectedIndex<0&&chip.Items.Count>0)chip.SelectedIndex=0;Relayout();restoring=false;}
        async Task Query(){if(!(model.SelectedItem is JsonItem m)||!(part.SelectedItem is JsonItem p)||program.SelectedItem==null||!(status.SelectedItem is StatusItem st)){message.Text="请选择完整条件";return;}SetBusy(true);try{string pr=program.SelectedItem.ToString(),purpose=chip.Visible?(chip.SelectedItem?.ToString()??""):"",addr=pr=="BootLoader"?"0x08000000":AuthClient.Text(p.Value,"burn_addr");ParseAddress(addr);string ac=auth.AfterSales?aircraft.Text.Trim():"",en=auth.AfterSales?eo.Text.Trim():"";version=await auth.LatestAsync(AuthClient.Text(m.Value,"model_code"),AuthClient.Text(p.Value,"part_no"),purpose,pr,st.Value,ac,en);config.Update(("model_code",AuthClient.Text(m.Value,"model_code")),("part_no",AuthClient.Text(p.Value,"part_no")),("purpose",purpose),("program",pr),("status",st.Value),("aircraft_no",ac),("eo_no",en));if(version==null){message.Text="没有匹配的固件";ClearInfo();return;}burnAddress=addr;ShowVersion(version.Value);burn.Enabled=true;message.Text="查询成功，可以烧录";}catch(Exception e){message.Text="查询失败："+e.Message;ClearVersion();}finally{SetBusy(false);}}
        void ShowVersion(JsonElement v){foreach(var k in new[]{"file_name","file_md5","version"})info[k].Text=AuthClient.Text(v,k);info["file_size"].Text=FormatSize(AuthClient.Text(v,"file_size"));info["burn_addr"].Text=burnAddress;}
        void Burn(){if(version==null)return;var v=version.Value;string cached=null;var pf=new ProgressForm("STM32 ST-Link 烧录过程","烧录",async log=>{bool attempted=false,ok=false;try{if(cached==null)cached=await auth.DownloadAsync(v,log);await Task.Run(()=>{TextWriter oldOut=Console.Out,oldErr=Console.Error;var w=new LogWriter(log);Console.SetOut(w);Console.SetError(w);var p=new STM32Programmer();try{attempted=true;p.FlashFirmware(cached,ParseAddress(burnAddress),true,true,null);ok=true;}finally{try{p.Close();}catch(Exception e){log("[!] 关闭ST-Link时出错: "+e.Message+"\n");}Console.SetOut(oldOut);Console.SetError(oldErr);}});}catch(Exception e){log("[✗] 烧录失败: "+e.Message+"\n");}if(attempted)try{await auth.SubmitBurnAsync(v,ok);log("[✓] 烧录记录已提交\n");}catch(Exception e){log("[!] 烧录记录提交失败: "+e.Message+"\n");}return ok;});pf.ShowDialog(this);}
        void Erase(){if(MessageBox.Show(this,"全片擦除将永久删除芯片内全部程序，操作不可撤销。是否继续？","确认全片擦除",MessageBoxButtons.YesNo,MessageBoxIcon.Warning)!=DialogResult.Yes)return;var pf=new ProgressForm("STM32 ST-Link 全片擦除过程","全片擦除",async log=>await Task.Run(()=>{TextWriter oldOut=Console.Out,oldErr=Console.Error;var w=new LogWriter(log);Console.SetOut(w);Console.SetError(w);var p=new STM32Programmer();try{p.Connect();p.ErasePages(0x08000000,0x100000);log("[✓] 全片擦除完成\n");return true;}catch(Exception e){log("[✗] 全片擦除失败: "+e.Message+"\n");return false;}finally{try{p.Close();}catch(Exception e){log("[!] 关闭ST-Link时出错: "+e.Message+"\n");}Console.SetOut(oldOut);Console.SetError(oldErr);}}));pf.ShowDialog(this);}
        void SetBusy(bool b){query.Enabled=erase.Enabled=!b;model.Enabled=part.Enabled=chip.Enabled=program.Enabled=status.Enabled=!b;aircraft.Enabled=eo.Enabled=!b;}void ClearVersion(){if(restoring)return;version=null;burn.Enabled=false;ClearInfo();}void ClearInfo(){foreach(var l in info.Values)l.Text="-";}
        static void SelectText(ComboBox b,string v){for(int i=0;i<b.Items.Count;i++)if(string.Equals(b.Items[i].ToString(),v,StringComparison.Ordinal)){b.SelectedIndex=i;return;}if(b.Items.Count>0)b.SelectedIndex=0;}static void SelectJson(ComboBox b,string key,string v){for(int i=0;i<b.Items.Count;i++)if(b.Items[i] is JsonItem x&&AuthClient.Text(x.Value,key)==v){b.SelectedIndex=i;return;}}
        void SelectStatus(int v){for(int i=0;i<status.Items.Count;i++)if(status.Items[i] is StatusItem x&&x.Value==v){status.SelectedIndex=i;return;}if(status.Items.Count>0)status.SelectedIndex=0;}
        static uint ParseAddress(string x){if(string.IsNullOrWhiteSpace(x))throw new Exception("零部件缺少burn_addr");x=x.Trim();return x.StartsWith("0x",StringComparison.OrdinalIgnoreCase)?Convert.ToUInt32(x.Substring(2),16):Convert.ToUInt32(x,CultureInfo.InvariantCulture);}static string FormatSize(string x){if(!double.TryParse(x,out var n))return x;string[] u={"B","KB","MB","GB","TB"};int i=0;while(n>=1024&&i<u.Length-1){n/=1024;i++;}return i==0?$"{n:0} {u[i]}":$"{n:0.0} {u[i]}";}sealed class JsonItem{public JsonElement Value;readonly string text;public JsonItem(JsonElement v,string t){Value=v.Clone();text=t;}public override string ToString()=>text;}sealed class StatusItem{public int Value;readonly string label;public StatusItem(int v,string l){Value=v;label=l;}public override string ToString()=>label;}
    }

    static class Program
    {
        [STAThread] static int Main(string[] args)
        {
            if (args.Any(x => string.Equals(x, "--self-test", StringComparison.OrdinalIgnoreCase)))
            {
                using var test = new AuthClient();
                if (test.AfterSales || test.AllowedPrograms().Count != 3 || test.AllowedStatuses().Count != 3) return 2;
                string dir=Path.Combine(Path.GetTempPath(),"stm32-config-test-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(dir);string file=Path.Combine(dir,"config.json");
                try{var c=new AppConfig(file);c.Update(("username","tester"),("model_code","EH216-S"),("status",1),("password","secret"),("api_token","token"));var loaded=new AppConfig(file);if(loaded.Get("username")!="tester"||loaded.Get("model_code")!="EH216-S"||loaded.GetInt("status",-1)!=1)return 3;string raw=File.ReadAllText(file);if(raw.Contains("secret")||raw.Contains("token"))return 4;}finally{try{Directory.Delete(dir,true);}catch{}}
                return 0;
            }
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.ThreadException += (_, e) => MessageBox.Show(e.Exception.Message, "程序错误", MessageBoxButtons.OK, MessageBoxIcon.Error);
            var config = new AppConfig();
            using var auth = new AuthClient();
            using (var login = new LoginForm(auth, config)) if (login.ShowDialog() != DialogResult.OK) return 0;
            Application.Run(new FirmwareForm(auth, config));
            return 0;
        }
    }
}
